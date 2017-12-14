#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
CCSD analytical nuclear gradients
'''

import time
import ctypes
import numpy
from pyscf import lib
from functools import reduce
from pyscf.lib import logger
from pyscf import gto
from pyscf import ao2mo
from pyscf.cc import ccsd
from pyscf.cc import _ccsd
from pyscf.cc import ccsd_rdm
from pyscf.scf import rhf_grad
from pyscf.scf import cphf


#
# Note: only works with canonical orbitals
# Non-canonical formula refers to JCP, 95, 2639
#
def kernel(mycc, t1=None, t2=None, l1=None, l2=None, eris=None, atmlst=None,
           mf_grad=None, d1=None, d2=None, verbose=logger.INFO):
    if eris is not None:
        if abs(eris.fock - numpy.diag(eris.fock.diagonal())).max() > 1e-3:
            raise RuntimeError('CCSD gradients does not support NHF (non-canonical HF)')

    if t1 is None: t1 = mycc.t1
    if t2 is None: t2 = mycc.t2
    if l1 is None: l1 = mycc.l1
    if l2 is None: l2 = mycc.l2
    if mf_grad is None:
        mf_grad = rhf_grad.Gradients(mycc._scf)

    log = logger.new_logger(mycc, verbose)
    time0 = time.clock(), time.time()

    log.debug('Build ccsd rdm1 intermediates')
    if d1 is None:
        d1 = ccsd_rdm.gamma1_intermediates(mycc, t1, t2, l1, l2)
    doo, dov, dvo, dvv = d1
    time1 = log.timer_debug1('rdm1 intermediates', *time0)
    log.debug('Build ccsd rdm2 intermediates')
    fdm2 = lib.H5TmpFile()
    if d2 is None:
        d2 = ccsd_rdm._gamma2_outcore(mycc, t1, t2, l1, l2, fdm2)
    time1 = log.timer_debug1('rdm2 intermediates', *time1)

    mol = mycc.mol
    mo_coeff = mycc.mo_coeff
    mo_energy = mycc._scf.mo_energy
    nao, nmo = mo_coeff.shape
    nocc = numpy.count_nonzero(mycc.mo_occ > 0)
    nvir = nmo - nocc
    nao_pair = nao * (nao+1) // 2
    with_frozen = not (mycc.frozen is None or mycc.frozen is 0)
    OA, VA, OF, VF = index_frozen_active(mycc)

    log.debug('symmetrized rdm2 and MO->AO transformation')
# Roughly, dm2*2 is computed in _rdm2_mo2ao
    mo_active = mo_coeff[:,numpy.hstack((OA,VA))]
    _rdm2_mo2ao(mycc, d2, mo_active, fdm2)  # transform the active orbitals
    time1 = log.timer_debug1('MO->AO transformation', *time1)
    hf_dm1 = mycc._scf.make_rdm1(mycc.mo_coeff, mycc.mo_occ)

    if atmlst is None:
        atmlst = range(mol.natm)
    offsetdic = mol.offset_nr_by_atom()
    diagidx = numpy.arange(nao)
    diagidx = diagidx*(diagidx+1)//2 + diagidx
    de = numpy.zeros((len(atmlst),3))
    Imat = numpy.zeros((nao,nao))
    vhf1 = fdm2.create_dataset('vhf1', (len(atmlst),3,nao,nao), 'f8')

# 2e AO integrals dot 2pdm
    max_memory = max(0, mycc.max_memory - lib.current_memory()[0])
    blksize = max(1, int(max_memory*1e6/8/(nao**3*2.5)))
    ioblksize = fdm2['dm2'].chunks[1]

    for k, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = offsetdic[ia]
        ip1 = p0
        vhf = 0
        for b0, b1, nf in shell_prange(mol, shl0, shl1, blksize):
            ip0, ip1 = ip1, ip1 + nf
            dm2buf = numpy.empty((nf,nao,nao_pair))
            for i0, i1 in lib.prange(0, nao_pair, ioblksize):
                _load_block_tril(fdm2['dm2'], ip0, ip1, i0, i1, dm2buf[:,:,i0:i1])
            dm2buf[:,:,diagidx] *= .5
            shls_slice = (b0,b1,0,mol.nbas,0,mol.nbas,0,mol.nbas)
            eri0 = mol.intor('int2e', aosym='s2kl', shls_slice=shls_slice)
            Imat += lib.einsum('ipx,iqx->pq', eri0.reshape(nf,nao,-1), dm2buf)
            eri0 = None

            eri1 = mol.intor('int2e_ip1', comp=3, aosym='s2kl',
                             shls_slice=shls_slice).reshape(3,nf,nao,-1)
            de[k] -= numpy.einsum('xijk,ijk->x', eri1, dm2buf) * 2
            dm2buf = None
# HF part
            eri1 = lib.unpack_tril(eri1.reshape(3*nf*nao,-1)).reshape(3,nf,nao,nao,nao)
            vhf += numpy.einsum('xijkl,ij->xkl', eri1, hf_dm1[ip0:ip1])
            vhf -= numpy.einsum('xijkl,il->xkj', eri1, hf_dm1[ip0:ip1]) * .5
            vhf[:,ip0:ip1] += numpy.einsum('xijkl,kl->xij', eri1, hf_dm1)
            vhf[:,ip0:ip1] -= numpy.einsum('xijkl,jk->xil', eri1, hf_dm1) * .5
            eri1 = None
        vhf1[k] = vhf
        log.debug('2e-part grad of atom %d %s = %s', ia, mol.atom_symbol(ia), de[k])
        time1 = log.timer_debug1('2e-part grad of atom %d'%ia, *time1)

    Imat = reduce(numpy.dot, (mo_coeff.T, Imat, mycc._scf.get_ovlp(), mo_coeff)) * -1

    dm1mo = numpy.zeros((nmo,nmo))
    if with_frozen:
        OA, VA, OF, VF = index_frozen_active(mycc)
        dco = (Imat[OF[:,None],OA]
               / lib.direct_sum('i-j->ij', mo_energy[OF], mo_energy[OA]))
        dfv = (Imat[VF[:,None],VA]
               / lib.direct_sum('a-b->ab', mo_energy[VF], mo_energy[VA]))
        dm1mo[OA[:,None],OA] = doo + doo.T
        dm1mo[OF[:,None],OA] = dco
        dm1mo[OA[:,None],OF] = dco.T
        dm1mo[VA[:,None],VA] = dvv + dvv.T
        dm1mo[VF[:,None],VA] = dfv
        dm1mo[VA[:,None],VF] = dfv.T
    else:
        dm1mo[:nocc,:nocc] = doo + doo.T
        dm1mo[nocc:,nocc:] = dvv + dvv.T

    dm1 = reduce(numpy.dot, (mo_coeff, dm1mo, mo_coeff.T))
    vj, vk = mycc._scf.get_jk(mycc.mol, dm1)
    Xvo = reduce(numpy.dot, (mo_coeff[:,nocc:].T, vj*2-vk, mo_coeff[:,:nocc]))
    Xvo+= Imat[:nocc,nocc:].T - Imat[nocc:,:nocc]

    dm1mo += _response_dm1(mycc, Xvo, eris)
    dm1 = reduce(numpy.dot, (mo_coeff, dm1mo, mo_coeff.T))
    time1 = log.timer_debug1('response_rdm1 intermediates', *time1)

    Imat[nocc:,:nocc] = Imat[:nocc,nocc:].T
    im1 = reduce(numpy.dot, (mo_coeff, Imat, mo_coeff.T))
    time1 = log.timer_debug1('response_rdm1', *time1)

    log.debug('h1 and JK1')
    h1 = mf_grad.get_hcore(mol)
    s1 = mf_grad.get_ovlp(mol)
    zeta = lib.direct_sum('i+j->ij', mo_energy, mo_energy) * .5
    zeta[nocc:,:nocc] = mo_energy[:nocc]
    zeta[:nocc,nocc:] = mo_energy[:nocc].reshape(-1,1)
    zeta = reduce(numpy.dot, (mo_coeff, zeta*dm1mo, mo_coeff.T))
    p1 = numpy.dot(mo_coeff[:,:nocc], mo_coeff[:,:nocc].T)
    vhf4sij = reduce(numpy.dot, (p1, mycc._scf.get_veff(mol, dm1+dm1.T), p1))
    time1 = log.timer_debug1('h1 and JK1', *time1)

    # Hartree-Fock part contribution
    dm1p = hf_dm1 + dm1*2
    dm1 += hf_dm1
    zeta += mf_grad.make_rdm1e(mycc._scf.mo_energy, mycc._scf.mo_coeff,
                               mycc._scf.mo_occ)

    for k, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = offsetdic[ia]
# s[1] dot I, note matrix im1 is not hermitian
        de[k] += numpy.einsum('xij,ij->x', s1[:,p0:p1], im1[p0:p1])
        de[k] += numpy.einsum('xji,ij->x', s1[:,p0:p1], im1[:,p0:p1])
# h[1] \dot DM, *2 for +c.c.,  contribute to f1
        h1ao = mf_grad._grad_rinv(mol, ia)
        h1ao[:,p0:p1] += h1[:,p0:p1]
        de[k] += numpy.einsum('xij,ij->x', h1ao, dm1)
        de[k] += numpy.einsum('xji,ij->x', h1ao, dm1)
# -s[1]*e \dot DM,  contribute to f1
        de[k] -= numpy.einsum('xij,ij->x', s1[:,p0:p1], zeta[p0:p1]  )
        de[k] -= numpy.einsum('xji,ij->x', s1[:,p0:p1], zeta[:,p0:p1])
# -vhf[s_ij[1]],  contribute to f1, *2 for s1+s1.T
        de[k] -= numpy.einsum('xij,ij->x', s1[:,p0:p1], vhf4sij[p0:p1]) * 2
        de[k] -= numpy.einsum('xij,ij->x', vhf1[k], dm1p)

    de += rhf_grad.grad_nuc(mol)
    log.timer('%s gradients' % mycc.__class__.__name__, *time0)
    return de


def as_scanner(grad_cc):
    '''Generating a nuclear gradients scanner/solver (for geometry optimizer).

    The returned solver is a function. This function requires one argument
    "mol" as input and returns total CCSD energy.

    The solver will automatically use the results of last calculation as the
    initial guess of the new calculation.  All parameters assigned in the
    CCSD and the underlying SCF objects (conv_tol, max_memory etc) are
    automatically applied in the solver.

    Note scanner has side effects.  It may change many underlying objects
    (_scf, with_df, with_x2c, ...) during calculation.

    Examples::

        >>> from pyscf import gto, scf, cc
        >>> mol = gto.M(atom='H 0 0 0; F 0 0 1')
        >>> cc_scanner = cc.CCSD(scf.RHF(mol)).nuc_grad_method().as_scanner()
        >>> e_tot, grad = cc_scanner(gto.M(atom='H 0 0 0; F 0 0 1.1'))
        >>> e_tot, grad = cc_scanner(gto.M(atom='H 0 0 0; F 0 0 1.5'))
    '''
    logger.info(grad_cc, 'Set nuclear gradients of %s as a scanner', grad_cc.__class__)
    class CCSD_GradScanner(grad_cc.__class__, lib.GradScanner):
        def __init__(self, g):
            self.__dict__.update(g.__dict__)
            self._cc = grad_cc._cc.as_scanner()
        def __call__(self, mol, **kwargs):
            # The following simple version also works.  But eris object is
            # recomputed in cc_scanner and solve_lambda.
            # cc = self._cc
            # cc(mol)
            # eris = cc.ao2mo()
            # cc.solve_lambda(cc.t1, cc.t2, cc.l1, cc.l2, eris=eris)
            # mf_grad = cc._scf.nuc_grad_method()
            # de = self.kernel(cc.t1, cc.t2, cc.l1, cc.l2, eris=eris, mf_grad=mf_grad)

            cc = self._cc
            mf_scanner = cc._scf
            mf_scanner(mol)
            cc.mol = mol
            cc.mo_coeff = mf_scanner.mo_coeff
            cc.mo_occ = mf_scanner.mo_occ
            eris = cc.ao2mo(cc.mo_coeff)
            cc.kernel(cc.t1, cc.t2, eris=eris)
            cc.solve_lambda(cc.t1, cc.t2, cc.l1, cc.l2, eris=eris)
            mf_grad = mf_scanner.nuc_grad_method()
            de = self.kernel(cc.t1, cc.t2, cc.l1, cc.l2, eris=eris,
                             mf_grad=mf_grad, **kwargs)
            return cc.e_tot, de
        @property
        def converged(self):
            cc = self._cc
            return all((cc._scf.converged, cc.converged, cc.converged_lambda))
    return CCSD_GradScanner(grad_cc)


def shell_prange(mol, start, stop, blksize):
    nao = 0
    ib0 = start
    for ib in range(start, stop):
        now = (mol.bas_angular(ib)*2+1) * mol.bas_nctr(ib)
        nao += now
        if nao > blksize and nao > now:
            yield (ib0, ib, nao-now)
            ib0 = ib
            nao = now
    yield (ib0, stop, nao)

def _response_dm1(mycc, Xvo, eris=None):
    nvir, nocc = Xvo.shape
    nmo = nocc + nvir
    with_frozen = not (mycc.frozen is None or mycc.frozen is 0)
    if eris is None or with_frozen:
        mo_energy = mycc._scf.mo_energy
        mo_occ = mycc._scf.mo_occ
        def fvind(x):
            mo_coeff = mycc.mo_coeff
            x = x.reshape(Xvo.shape)
            dm = reduce(numpy.dot, (mo_coeff[:,nocc:], x, mo_coeff[:,:nocc].T))
            v = mycc._scf.get_veff(mycc.mol, dm + dm.T)
            v = reduce(numpy.dot, (mo_coeff[:,nocc:].T, v, mo_coeff[:,:nocc]))
            return v * 2
    else:
        mo_energy = eris.fock.diagonal()
        mo_occ = numpy.zeros_like(mo_energy)
        mo_occ[:nocc] = 2
        ovvo = numpy.empty((nocc,nvir,nvir,nocc))
        for i in range(nocc):
            ovvo[i] = eris.ovvo[i]
            ovvo[i] = ovvo[i] * 4 - ovvo[i].transpose(1,0,2)
            ovvo[i]-= eris.oovv[i].transpose(2,1,0)
        def fvind(x):
            return numpy.einsum('iabj,bj->ai', ovvo, x.reshape(Xvo.shape))
    dvo = cphf.solve(fvind, mo_energy, mo_occ, Xvo, max_cycle=30)[0]
    dm1 = numpy.zeros((nmo,nmo))
    dm1[nocc:,:nocc] = dvo
    dm1[:nocc,nocc:] = dvo.T
    return dm1

def _rdm2_mo2ao(mycc, d2, mo_coeff, fsave=None):
# dm2 = ccsd_rdm._make_rdm2(mycc, t1, t2, l1, l2)
# dm2 = numpy.einsum('pi,ijkl->pjkl', mo_coeff, dm2)
# dm2 = numpy.einsum('pj,ijkl->ipkl', mo_coeff, dm2)
# dm2 = numpy.einsum('pk,ijkl->ijpl', mo_coeff, dm2)
# dm2 = numpy.einsum('pl,ijkl->ijkp', mo_coeff, dm2)
# dm2 = dm2 + dm2.transpose(1,0,2,3)
# dm2 = dm2 + dm2.transpose(0,1,3,2)
# return ao2mo.restore(4, dm2*.5, nmo)
    log = logger.Logger(mycc.stdout, mycc.verbose)
    time1 = time.clock(), time.time()
    if fsave is None:
        incore = True
        fsave = lib.H5TmpFile()
    else:
        incore = False
    dovov, dvvvv, doooo, doovv, dovvo, dvvov, dovvv, dooov = d2

    nocc, nvir = dovov.shape[:2]
    nov = nocc * nvir
    mo_coeff = numpy.asarray(mo_coeff, order='F')
    nao, nmo = mo_coeff.shape
    nao_pair = nao * (nao+1) // 2
    nvir_pair = nvir * (nvir+1) //2

    def _trans(vin, orbs_slice, out=None):
        nrow = vin.shape[0]
        if out is None:
            out = numpy.empty((nrow,nao_pair))
        fdrv = getattr(_ccsd.libcc, 'AO2MOnr_e2_drv')
        pao_loc = ctypes.POINTER(ctypes.c_void_p)()
        fdrv(_ccsd.libcc.AO2MOtranse2_nr_s1, _ccsd.libcc.CCmmm_transpose_sum,
             out.ctypes.data_as(ctypes.c_void_p),
             vin.ctypes.data_as(ctypes.c_void_p),
             mo_coeff.ctypes.data_as(ctypes.c_void_p),
             ctypes.c_int(nrow), ctypes.c_int(nao),
             (ctypes.c_int*4)(*orbs_slice), pao_loc, ctypes.c_int(0))
        return out

    fswap = lib.H5TmpFile()
    max_memory = mycc.max_memory - lib.current_memory()[0]
    blksize = int(max_memory*1e6/8/(nao_pair+nmo**2))
    blksize = min(nvir_pair, max(ccsd.BLKMIN, blksize))
    fswap.create_dataset('v', (nao_pair,nvir_pair), 'f8', chunks=(nao_pair,blksize))
    for p0, p1 in lib.prange(0, nvir_pair, blksize):
        fswap['v'][:,p0:p1] = _trans(lib.unpack_tril(_cp(dvvvv[p0:p1])),
                                     (nocc,nmo,nocc,nmo)).T
    time1 = log.timer_debug1('_rdm2_mo2ao pass 1', *time1)

# transform dm2_ij to get lower triangular (dm2+dm2.transpose(0,1,3,2))
    blksize = int(max_memory*1e6/8/(nao_pair+nmo**2))
    blksize = min(nao_pair, max(ccsd.BLKMIN, blksize))
    fswap.create_dataset('o', (nmo,nocc,nao_pair), 'f8', chunks=(nmo,nocc,blksize))
    buf1 = numpy.zeros((nocc,nocc,nmo,nmo))
    buf1[:,:,:nocc,:nocc] = doooo
    buf1[:,:,nocc:,nocc:] = _cp(doovv)
    buf1 = _trans(buf1.reshape(nocc**2,-1), (0,nmo,0,nmo))
    fswap['o'][:nocc] = buf1.reshape(nocc,nocc,nao_pair)
    dovoo = numpy.asarray(dooov).transpose(2,3,0,1)
    for p0, p1 in lib.prange(nocc, nmo, nocc):
        buf1 = numpy.zeros((nocc,p1-p0,nmo,nmo))
        buf1[:,:,:nocc,:nocc] = dovoo[:,p0-nocc:p1-nocc]
        buf1[:,:,nocc:,:nocc] = dovvo[:,p0-nocc:p1-nocc]
        buf1[:,:,:nocc,nocc:] = dovov[:,p0-nocc:p1-nocc]
        buf1[:,:,nocc:,nocc:] = dovvv[:,p0-nocc:p1-nocc]
        buf1 = buf1.transpose(1,0,3,2).reshape((p1-p0)*nocc,-1)
        buf1 = _trans(buf1, (0,nmo,0,nmo))
        fswap['o'][p0:p1] = buf1.reshape(p1-p0,nocc,nao_pair)
    time1 = log.timer_debug1('_rdm2_mo2ao pass 2', *time1)
    dovoo = buf1 = None

# transform dm2_kl then dm2 + dm2.transpose(2,3,0,1)
    gsave = fsave.create_dataset('dm2', (nao_pair,nao_pair), 'f8', chunks=(nao_pair,blksize))
    diagidx = numpy.arange(nao)
    diagidx = diagidx*(diagidx+1)//2 + diagidx
    for p0, p1 in lib.prange(0, nao_pair, blksize):
        buf1 = numpy.zeros((p1-p0,nmo,nmo))
        buf1[:,nocc:,nocc:] = lib.unpack_tril(_cp(fswap['v'][p0:p1]))
        buf1[:,:,:nocc] = fswap['o'][:,:,p0:p1].transpose(2,0,1)
        buf2 = _trans(buf1, (0,nmo,0,nmo))
        ic = 0
        idx = diagidx[diagidx<p1]
        if p0 > 0:
            buf1 = _cp(gsave[:p0,p0:p1])
            buf1[:p0,:p1-p0] += buf2[:p1-p0,:p0].T
            buf2[:p1-p0,:p0] = buf1[:p0,:p1-p0].T
            gsave[:p0,p0:p1] = buf1
        lib.transpose_sum(buf2[:,p0:p1], inplace=True)
        gsave[p0:p1] = buf2
    time1 = log.timer_debug1('_rdm2_mo2ao pass 3', *time1)
    if incore:
        return fsave['dm2'].value
    else:
        return fsave

#
# .
# . .
# ----+             -----------
# ----|-+       =>  -----------
# . . | | .
# . . | | . .
#
def _load_block_tril(dat, row0, row1, col0, col1, out=None):
    shape = dat.shape
    nd = int(numpy.sqrt(shape[0]*2))
    if out is None:
        out = numpy.empty((row1-row0,nd,col1-col0)+shape[2:])
    dat1 = dat[row0*(row0+1)//2:row1*(row1+1)//2,col0:col1]
    p1 = 0
    for i in range(row0, row1):
        p0, p1 = p1, p1 + i+1
        out[i-row0,:i+1] = dat1[p0:p1]
        for j in range(row0, i):
            out[j-row0,i] = out[i-row0,j]
    for i in range(row1, nd):
        i2 = i*(i+1)//2
        out[:,i] = dat[i2+row0:i2+row1,col0:col1]
    return out

def _cp(a):
    return numpy.array(a, copy=False, order='C')

def index_frozen_active(cc):
    nocc = numpy.count_nonzero(cc.mo_occ > 0)
    moidx = cc.get_frozen_mask()
    OA = numpy.where( moidx[:nocc])[0] # occupied active orbitals
    OF = numpy.where(~moidx[:nocc])[0] # occupied frozen orbitals
    VA = numpy.where( moidx[nocc:])[0] + nocc # virtual active orbitals
    VF = numpy.where(~moidx[nocc:])[0] + nocc # virtual frozen orbitals
    return OA, VA, OF, VF

class Gradients(lib.StreamObject):
    def __init__(self, mycc):
        self._cc = mycc
        self.mol = mycc.mol
        self.stdout = mycc.stdout
        self.verbose = mycc.verbose
        self.atmlst = range(mycc.mol.natm)
        self.de = None

    def kernel(self, t1=None, t2=None, l1=None, l2=None, eris=None,
               atmlst=None, mf_grad=None, verbose=None):
        log = logger.new_logger(self, verbose)
        if t1 is None: t1 = self._cc.t1
        if t2 is None: t2 = self._cc.t2
        if l1 is None: l1 = self._cc.l1
        if l2 is None: l2 = self._cc.l2
        if eris is None:
            eris = self._cc.ao2mo()
        if t1 is None or t2 is None:
            t1, t2 = self._cc.kernel(eris=eris)
        if l1 is None or l2 is None:
            l1, l2 = self._cc.solve_lambda(eris=eris)
        if atmlst is None:
            atmlst = self.atmlst
        else:
            self.atmlst = atmlst

        self.de = kernel(self._cc, t1, t2, l1, l2, eris, atmlst,
                         mf_grad, verbose=log)
        if self.verbose >= logger.NOTE:
            log.note('--------------- %s gradients ---------------',
                     self.__class__.__name__)
            rhf_grad._write(self, self.mol, self.de, atmlst)
            log.note('----------------------------------------------')
        return self.de

    as_scanner = as_scanner


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf
    from pyscf import ao2mo
    from pyscf import grad

    mol = gto.M(
        atom = [
            ["O" , (0. , 0.     , 0.)],
            [1   , (0. ,-0.757  , 0.587)],
            [1   , (0. , 0.757  , 0.587)]],
        basis = '631g'
    )
    mf = scf.RHF(mol)
    ehf = mf.scf()

    mycc = ccsd.CCSD(mf)
    mycc.kernel()
    g1 = Gradients(mycc).kernel()
#[[ 0   0                1.00950925e-02]
# [ 0   2.28063426e-02  -5.04754623e-03]
# [ 0  -2.28063426e-02  -5.04754623e-03]]
    print(lib.finger(g1) - -0.036999389889460096)

    print('-----------------------------------')
    mol = gto.M(
        atom = [
            ["O" , (0. , 0.     , 0.)],
            [1   , (0. ,-0.757  , 0.587)],
            [1   , (0. , 0.757  , 0.587)]],
        basis = '631g'
    )
    mf = scf.RHF(mol)
    ehf = mf.scf()

    mycc = ccsd.CCSD(mf)
    mycc.frozen = [0,1,10,11,12]
    mycc.max_memory = 1
    mycc.kernel()
    g1 = Gradients(mycc).kernel()
#[[ -7.81105940e-17   3.81840540e-15   1.20415540e-02]
# [  1.73095055e-16  -7.94568837e-02  -6.02077699e-03]
# [ -9.49844615e-17   7.94568837e-02  -6.02077699e-03]]
    print(lib.finger(g1) - 0.10599632044533455)

    mol = gto.M(
        atom = 'H 0 0 0; H 0 0 1.76',
        basis = '631g',
        unit='Bohr')
    mf = scf.RHF(mol).run(conv_tol=1e-14)
    mycc = ccsd.CCSD(mf)
    mycc.conv_tol = 1e-10
    mycc.conv_tol_normt = 1e-10
    mycc.kernel()
    g1 = Gradients(mycc).kernel()
#[[ 0.          0.         -0.07080036]
# [ 0.          0.          0.07080036]]
