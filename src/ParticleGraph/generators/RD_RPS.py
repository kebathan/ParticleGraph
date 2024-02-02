import torch
import torch_geometric as pyg
from ParticleGraph.utils import to_numpy


class RD_RPS(pyg.nn.MessagePassing):
    """Interaction Network as proposed in this paper:
    https://proceedings.neurips.cc/paper/2016/hash/3147da8ab4a0437c15ef51a5cc7f2dc4-Abstract.html"""

    """
    Compute the reaction diffusion according to the rock paper scissor model.

    Inputs
    ----------
    data : a torch_geometric.data object
    Note the Laplacian coeeficients are in data.edge_attr

    Returns
    -------
    increment : float
        the first derivative of three scalar fields u, v and w
        
    """

    def __init__(self, aggr_type=[], c=[], beta=[], bc_diff=[]):
        super(RD_RPS, self).__init__(aggr='add')  # "mean" aggregation.

        self.c = c
        self.beta = beta
        self.bc_diff = bc_diff

        self.D = 0.05
        self.a = 0.6

    def forward(self, data):
        c = self.c[to_numpy(data.x[:, 5])]
        c = c[:, None]

        uvw = data.x[:, 6:9]
        laplace_uvw = self.beta * c * self.propagate(data.edge_index, uvw=uvw, edge_attr=data.edge_attr)
        p = torch.sum(uvw, axis=1)

        # This is equivalent to the nonlinear reaction diffusion equation:
        #   du = D * laplace_u + u * (1 - p - a * v)
        #   dv = D * laplace_v + v * (1 - p - a * w)
        #   dw = D * laplace_w + w * (1 - p - a * u)
        d_uvw = self.D * laplace_uvw + uvw * (1 - p - self.a * uvw[:, [1, 2, 0]])

        return d_uvw

    def message(self, uvw_i, uvw_j, edge_attr):
        return edge_attr * uvw_j

    def psi(self, I, p):
        return I
