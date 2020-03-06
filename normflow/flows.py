import numpy as np
import torch
import torch.nn as nn
from normflow import nets



# flows
class Flow(nn.Module):
    """
    Generic class for flow functions
    """
    def __init__(self):
        super().__init__()

    def forward(self, z):
        raise NotImplementedError('Forward pass has not been implemented.')

    def inverse(self, z):
        raise NotImplementedError('This flow has no algebraic inverse.')


class Planar(Flow):
    """
    Planar flow as introduced in arXiv: 1505.05770
        f(z) = z + u * h(w * z + b)
    """
    def __init__(self, shape, h=torch.tanh, u=None, w=None, b=None):
        """
        Constructor of the planar flow
        :param shape: shape of the latent variable z
        :param h: nonlinear function h of the planar flow (see definition of f above)
        :param u,w,b: optional initialization for parameters
        """
        super().__init__()
        lim = 3. / np.prod(shape)
        
        if u is not None:
            self.u = nn.Parameter(u)
        else:
            self.u = nn.Parameter(torch.empty(shape)[(None,) * 2])
            nn.init.uniform_(self.u, -lim, lim)
        if w is not None:
            self.w = nn.Parameter(w)
        else:
            self.w = nn.Parameter(torch.empty(shape)[(None,) * 2])
            nn.init.uniform_(self.w, -lim, lim)
        if b is not None:
            self.b = nn.Parameter(b)
        else:
            self.b = nn.Parameter(torch.empty(1))
            nn.init.uniform_(self.b, -lim, lim)
        self.h = h

    def forward(self, z):
        if self.h == torch.tanh:
            inner = torch.sum(self.w * self.u)
            u = self.u + (torch.log(1 + torch.exp(inner)) - 1 - inner) * self.w / torch.sum(self.w ** 2)
            h_ = lambda x: 1 / torch.cosh(x) ** 2
        else:
            raise NotImplementedError('Nonlinearity is not implemented.')
        lin = torch.sum(self.w * z, list(range(2, self.w.dim())), keepdim=True) + self.b
        z_ = z + u * self.h(lin)
        log_det = torch.log(torch.abs(1 + torch.sum(self.w * u) * h_(lin.squeeze())))
        if log_det.dim() == 0:
            log_det = log_det.unsqueeze(0)
        if log_det.dim() == 1:
            log_det = log_det.unsqueeze(1)
        return z_, log_det


class Radial(Flow):
    """
    Radial flow as introduced in arXiv: 1505.05770
        f(z) = z + beta * h(alpha, r) * (z - z_0)
    """
    def __init__(self, shape, z_0=None):
        """
        Constructor of the radial flow
        :param shape: shape of the latent variable z
        :param z_0: parameter of the radial flow
        """
        super().__init__()
        self.d_cpu = torch.prod(torch.tensor(shape))
        self.register_buffer('d', self.d_cpu)
        self.beta = nn.Parameter(torch.empty(1))
        lim = 1.0 / np.prod(shape)
        nn.init.uniform_(self.beta, -lim - 1.0, lim - 1.0)
        self.alpha = nn.Parameter(torch.empty(1))
        nn.init.uniform_(self.alpha, -lim, lim)

        if z_0 is not None:
            self.z_0 = nn.Parameter(z_0)
        else:
            self.z_0 = nn.Parameter(torch.randn(shape)[(None,) * 2])

    def forward(self, z):
        beta = torch.log(1 + torch.exp(self.beta)) - torch.abs(self.alpha)
        dz = z - self.z_0
        r = torch.norm(dz, dim=list(range(2, self.z_0.dim())), keepdim=True)
        h_arr = beta / (torch.abs(self.alpha) + r)
        h_arr_ = - beta * r / (torch.abs(self.alpha) + r) ** 2
        z_ = z + h_arr * dz
        log_det = (self.d - 1) * torch.log(1 + h_arr) + torch.log(1 + h_arr + h_arr_)
        log_det = log_det.squeeze()
        if log_det.dim() == 0:
            log_det = log_det.unsqueeze(0)
        if log_det.dim() == 1:
            log_det = log_det.unsqueeze(1)
        return z_, log_det


class AffineConstFlow(Flow):
    """ 
    Scales + Shifts the flow by (learned) constants per dimension.
    In NICE paper there is a Scaling layer which is a special case of this where t is None
    """
    def __init__(self, shape, scale=True, shift=True):
        super().__init__()
        self.s = nn.Parameter(torch.randn(shape)[(None,) * 2]) if scale else None
        self.t = nn.Parameter(torch.randn(shape)[(None,) * 2]) if shift else None
        
    def forward(self, z):
        s = self.s if self.s is not None else z.new_zeros(z.size())
        t = self.t if self.t is not None else z.new_zeros(z.size())
        z_ = z * torch.exp(s) + t
        log_det = torch.sum(s, dim=2)
        return z_, log_det
    
    def inverse(self, z):
        s = self.s if self.s is not None else z.new_zeros(z.size())
        t = self.t if self.t is not None else z.new_zeros(z.size())
        z_ = (z - t) * torch.exp(-s)
        log_det = torch.sum(-s, dim=2)
        return z_, log_det
       
        
class ActNorm(AffineConstFlow):
    """
    An AffineConstFlow but with a data-dependent initialization,
    where on the very first batch we clever initialize the s,t so that the output
    is unit gaussian. As described in Glow paper.
    """
    def __init__(self):
        super().__init__()
        self.data_dep_init_done = False
    
    def forward(self, z):
        # first batch is used for init
        if not self.data_dep_init_done:
            assert self.s is not None and self.t is not None # for now
            self.s.data = (-torch.log(z.std(dim=0, keepdim=True))).detach()
            self.t.data = (-(z * torch.exp(self.s)).mean(dim=0, keepdim=True)).detach()
            self.data_dep_init_done = True
        return super().forward(z)


class AffineHalfFlow(Flow):
    """
    RealNVP as introduced in arXiv: 1605.08803
    affine autoregressive flow f(z) = z * exp(s) + t
    half of the dimensions in z are linearly scaled/transfromed as a function of the other half.
    Which half is which is determined by the parity bit.
    RealNVP both scales and shifts (default), NICE only shifts
    """
    def __init__(self, shape, parity, net_class=nets.MLP, nh=24, scale=True, shift=True):
        super().__init__()
        self.dim = shape[0]
        self.parity = parity
        self.s_cond = lambda x: x.new_zeros(x.size(0), x.size(1), self.dim // 2)
        self.t_cond = lambda x: x.new_zeros(x.size(0), x.size(1), self.dim // 2)
        if scale:
            self.s_cond = net_class([self.dim // 2, nh, nh, self.dim // 2])
        if shift:
            self.t_cond = net_class([self.dim // 2, nh, nh, self.dim // 2])
        
    def forward(self, z):
        z0, z1 = z[:, :, ::2], z[:, :, 1::2]
        print(z0)
        print(z1)
        bs = z.shape[0]
        if self.parity:
            z0, z1 = z1, z0
        s = self.s_cond(z0.reshape(-1, self.dim // 2))
        t = self.t_cond(z0.reshape(-1, self.dim // 2))
        z_0 = z0 # untouched
        z_1 = torch.exp(s.reshape(bs, -1, self.dim // 2)) * z1 + t.reshape(bs, -1, self.dim // 2)
        if self.parity:
            z_0, z_1 = z_1, z_0
        z_ = torch.cat([z_0, z_1], dim=2)
        log_det = torch.sum(s, dim=2)
        return z_, log_det
    
    def inverse(self, z):
        z0, z1 = z[:, :, ::2], z[:, :, 1::2]
        if self.parity:
            z0, z1 = z1, z0
        s = self.s_cond(z0)
        t = self.t_cond(z0)
        x0 = z0 # this was the same
        x1 = (z1 - t) * torch.exp(-s) # reverse the transform on this half
        if self.parity:
            x0, x1 = x1, x0
        x = torch.cat([x0, x1], dim=2)
        log_det = torch.sum(-s, dim=2)
        return x, log_det
    
    
class Invertible1x1Conv(Flow):
    def __init__(self, shape):
        super().__init__()
        self.dim = shape[0]
        Q = torch.nn.init.orthogonal_(torch.randn(self.dim, self.dim))
        P, L, U = torch.lu_unpack(*Q.lu())
        self.P = P # remains fixed during optimization
        self.L = nn.Parameter(L) # lower triangular portion
        self.S = nn.Parameter(U.diag()) # "crop out" the diagonal to its own parameter
        self.U = nn.Parameter(torch.triu(U, diagonal=1)) # "crop out" diagonal, stored in S

    def _assemble_W(self):
        # assemble W from its components (P, L, U, S)
        L = torch.tril(self.L, diagonal=-1) + torch.diag(torch.ones(self.dim))
        U = torch.triu(self.U, diagonal=1)
        W = self.P @ L @ (U + torch.diag(self.S))
        return W

    def forward(self, z):
        W = self._assemble_W()
        z_ = z @ W
        log_det = torch.sum(torch.log(torch.abs(self.S)))
        return z_, log_det

    def inverse(self, z):
        W = self._assemble_W()
        W_inv = torch.inverse(W)
        z_ = z @ W_inv
        log_det = -torch.sum(torch.log(torch.abs(self.S)))
        return z_, log_det


    
class Glow(Flow):
    """
    Glow: Generative Flow with Invertible 1×1 Convolutions, arXiv: 1807.03039
    It has a multi-scale architecture, each flow layer consists of three parts
    ActNorm(dim=2)
    Invertible1x1Conv(dim=2)
    AffineHalfFlow(dim=2, parity=i%2, nh=32)
    """
    def __init__(self, shape):
        """
        :param shape: shape of the latent variable z
        """
        super().__init__()
        self.flows = [ActNorm(shape), Invertible1x1Conv(shape), AffineHalfFlow(shape)]

    def forward(self, z):
        log_det_tot = 0
        for flow in self.flows:
            z, log_det = flow(z)
            log_det_tot -= log_det
        return z, log_det_tot


    """
    NICE as introduced in arXiv: 1410.8516
    AffineHalfFlow(dim=2, parity=i%2, scale=False)
    added a permutation of components of z for expressivity
    """
