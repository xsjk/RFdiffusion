"""SO(3) diffusion methods."""

import numpy as np
import torch
from scipy.spatial.transform import Rotation

### First define geometric operations on the SO3 manifold


# hat map from vector space R^3 to Lie algebra so(3)
def hat(v):
    hat_v = torch.zeros([v.shape[0], 3, 3], device=v.device)
    hat_v[:, 0, 1], hat_v[:, 0, 2], hat_v[:, 1, 2] = -v[:, 2], v[:, 1], -v[:, 0]
    return hat_v + -hat_v.transpose(2, 1)


# Logarithmic map from SO(3) to R^3 (i.e. rotation vector)
def Log(R):
    return torch.tensor(Rotation.from_matrix(R.cpu().numpy()).as_rotvec(), device=R.device)


# logarithmic map from SO(3) to so(3), this is the matrix logarithm
def log(R):
    return hat(Log(R))


# Exponential map from vector space of so(3) to SO(3), this is the matrix
# exponential combined with the "hat" map
def Exp(A):
    return torch.tensor(Rotation.from_rotvec(A.cpu().numpy()).as_matrix(), device=A.device)


# Angle of rotation SO(3) to R^+
def Omega(R):
    return torch.linalg.norm(log(R), axis=[-2, -1]) / np.sqrt(2.0)


L_default = 2000


def f_igso3(omega, t, L=L_default):
    """Truncated sum of IGSO(3) distribution.

    This function approximates the power series in equation 5 of
    "DENOISING DIFFUSION PROBABILISTIC MODELS ON SO(3) FOR ROTATIONAL
    ALIGNMENT"
    Leach et al. 2022

    This expression diverges from the expression in Leach in that here, sigma =
    sqrt(2) * eps, if eps_leach were the scale parameter of the IGSO(3).

    With this reparameterization, IGSO(3) agrees with the Brownian motion on
    SO(3) with t=sigma^2 when defined for the canonical inner product on SO3,
    <u, v>_SO3 = Trace(u v^T)/2

    Args:
        omega: i.e. the angle of rotation associated with rotation matrix
        t: variance parameter of IGSO(3), maps onto time in Brownian motion
        L: Truncation level
    """
    ls = torch.arange(L, device=omega.device)[None]  # of shape [1, L]
    return ((2 * ls + 1) * torch.exp(-ls * (ls + 1) * t / 2) * torch.sin(omega[:, None] * (ls + 1 / 2)) / torch.sin(omega[:, None] / 2)).sum(dim=-1)


def d_logf_d_omega(omega, t, L=L_default):
    omega = omega.clone().requires_grad_()
    log_f = torch.log(f_igso3(omega, t, L))
    return torch.autograd.grad(log_f.sum(), omega)[0]


# IGSO3 density with respect to the volume form on SO(3)
def igso3_density(Rt, t, L=L_default):
    return f_igso3(torch.tensor(Omega(Rt)), t, L).numpy()


def igso3_density_angle(omega, t, L=L_default):
    return f_igso3(omega, t, L) * (1 - torch.cos(omega)) / torch.pi


# grad_R log IGSO3(R; I_3, t)
def igso3_score(R, t, L=L_default):
    omega = Omega(R)
    unit_vector = np.einsum("Nij,Njk->Nik", R, log(R)) / omega[:, None, None]
    return unit_vector * d_logf_d_omega(omega, t, L)[:, None, None]


def calculate_igso3(*, num_sigma, num_omega, min_sigma, max_sigma):
    """calculate_igso3 pre-computes numerical approximations to the IGSO3 cdfs
    and score norms and expected squared score norms.

    Args:
        num_sigma: number of different sigmas for which to compute igso3
            quantities.
        num_omega: number of point in the discretization in the angle of
            rotation.
        min_sigma, max_sigma: the upper and lower ranges for the angle of
            rotation on which to consider the IGSO3 distribution.  This cannot
            be too low or it will create numerical instability.
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    # Discretize omegas for calculating CDFs. Skip omega=0.
    discrete_omega = torch.linspace(0, np.pi, num_omega + 1, device=device)[1:]

    # Exponential noise schedule.  This choice is closely tied to the
    # scalings used when simulating the reverse time SDE. For each step n,
    # discrete_sigma[n] = min_eps^(1-n/num_eps) * max_eps^(n/num_eps)
    discrete_sigma = 10 ** torch.linspace(np.log10(min_sigma), np.log10(max_sigma), num_sigma + 1, device=device)[1:]

    # Compute the pdf and cdf values for the marginal distribution of the angle
    # of rotation (which is needed for sampling)
    pdf_vals = torch.vstack([igso3_density_angle(discrete_omega, sigma**2) for sigma in discrete_sigma])
    cdf_vals = torch.cumsum(pdf_vals, dim=1) / num_omega * torch.pi

    # Compute the norms of the scores.  This are used to scale the rotation axis when
    # computing the score as a vector.
    score_norm = torch.vstack([d_logf_d_omega(discrete_omega, sigma**2) for sigma in discrete_sigma])

    # Compute the standard deviation of the score norm for each sigma
    exp_score_norms = torch.sqrt(torch.sum(score_norm**2 * pdf_vals, dim=1) / torch.sum(pdf_vals, dim=1))
    return {
        "cdf": cdf_vals.cpu().numpy(),
        "score_norm": score_norm.cpu().numpy(),
        "exp_score_norms": exp_score_norms.cpu().numpy(),
        "discrete_omega": discrete_omega.cpu().numpy(),
        "discrete_sigma": discrete_sigma.cpu().numpy(),
    }
