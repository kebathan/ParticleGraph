import torch
import numpy as np

from ParticleGraph.generators import PDE_A, PDE_A_bis, PDE_B, PDE_B_bis, PDE_E, PDE_G, PDE_Z, RD_Gray_Scott, RD_FitzHugh_Nagumo, RD_RPS, \
    Laplacian_A, PDE_O
from ParticleGraph.utils import choose_boundary_values


def choose_model(config, device):
    particle_model_name = config.graph_model.particle_model_name
    n_particle_types = config.simulation.n_particle_types
    aggr_type = config.graph_model.aggr_type
    bc_pos, bc_dpos = choose_boundary_values(config.simulation.boundary)

    params = config.simulation.params

    match particle_model_name:
        case 'PDE_A':
            p = torch.ones(n_particle_types, 4, device=device) + torch.rand(n_particle_types, 4, device=device)
            if params[0] != [-1]:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            else:
                print(p)
            sigma = config.simulation.sigma
            p = p if n_particle_types == 1 else torch.squeeze(p)
            model = PDE_A(aggr_type=aggr_type, p=torch.squeeze(p), sigma=sigma, bc_dpos=bc_dpos)
        case 'PDE_A_bis':
            p = torch.ones(n_particle_types, n_particle_types, 4, device=device) + torch.randn(n_particle_types, n_particle_types, 4, device=device)
            if params[0] != [-1]:
                for n in range(n_particle_types):
                    for m in range(n_particle_types):
                        p[n,m] = torch.tensor(params[n*3+m])
            else:
                print(p)
            sigma = config.simulation.sigma
            p = p if n_particle_types == 1 else torch.squeeze(p)
            model = PDE_A_bis(aggr_type=aggr_type, p=torch.squeeze(p), sigma=sigma, bc_dpos=bc_dpos)
        case 'PDE_B':
            p = torch.rand(n_particle_types, 3, device=device) * 100  # comprised between 10 and 50
            if params[0] != [-1]:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            else:
                print(p)
            model = PDE_B(aggr_type=aggr_type, p=torch.squeeze(p), bc_dpos=bc_dpos)
        case 'PDE_B_bis':
            p = torch.rand(n_particle_types, 3, device=device) * 100  # comprised between 10 and 50
            if params[0] != [-1]:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            else:
                print(p)
            model = PDE_B_bis(aggr_type=aggr_type, p=torch.squeeze(p), bc_dpos=bc_dpos)
        case 'PDE_G':
            if params[0] == [-1]:
                p = np.linspace(0.5, 5, n_particle_types)
                p = torch.tensor(p, device=device)
            if len(params) > 1:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            model = PDE_G(aggr_type=aggr_type, p=torch.squeeze(p), clamp=config.training.clamp,
                          pred_limit=config.training.pred_limit, bc_dpos=bc_dpos)
        case 'PDE_E':
            p = initialize_random_values(n_particle_types, device)
            if len(params) > 0:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            model = PDE_E(aggr_type=aggr_type, p=torch.squeeze(p),
                          clamp=config.training.clamp, pred_limit=config.training.pred_limit,
                          prediction=config.graph_model.prediction, bc_dpos=bc_dpos)
        case 'PDE_O':
            p = initialize_random_values(n_particle_types, device)
            if len(params) > 0:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            model = PDE_O(aggr_type=aggr_type, p=torch.squeeze(p), bc_dpos=bc_dpos, beta=config.simulation.beta)
        case 'Maze':
            p = torch.rand(n_particle_types, 3, device=device) * 100  # comprised between 10 and 50
            for n in range(n_particle_types):
                p[n] = torch.tensor(params[n])
            model = PDE_B(aggr_type=aggr_type, p=torch.squeeze(p), bc_dpos=bc_dpos)
        case _:
            model = PDE_Z()

    return model, bc_pos, bc_dpos


def choose_mesh_model(config, device):
    mesh_model_name = config.graph_model.mesh_model_name
    n_node_types = config.simulation.n_node_types
    aggr_type = config.graph_model.mesh_aggr_type
    _, bc_dpos = choose_boundary_values(config.simulation.boundary)

    c = initialize_random_values(n_node_types, device)
    for n in range(n_node_types):
        c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])

    beta = config.simulation.beta

    match mesh_model_name:
        case 'RD_Gray_Scott_Mesh':
            mesh_model = RD_Gray_Scott(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
        case 'RD_FitzHugh_Nagumo_Mesh':
            mesh_model = RD_FitzHugh_Nagumo(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
        case 'RD_RPS_Mesh':
            mesh_model = RD_RPS(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
        case 'DiffMesh' | 'WaveMesh':
            mesh_model = Laplacian_A(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
        case 'Chemotaxism_Mesh':
            c = initialize_random_values(n_node_types, device)
            for n in range(n_node_types):
                c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
            mesh_model = Laplacian_A(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
        case 'PDE_O_Mesh':
            c = initialize_random_values(n_node_types, device)
            for n in range(n_node_types):
                c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
            mesh_model = Laplacian_A(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
        case _:
            raise ValueError(f'Unknown model {model_name}')

    return mesh_model


# TODO: this seems to be used to provide default values in case no parameters are given?
def initialize_random_values(n, device):
    return torch.ones(n, 1, device=device) + torch.rand(n, 1, device=device)
