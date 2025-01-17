import torch
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
# matplotlib.use("Qt5Agg")
from tifffile import imread
from ParticleGraph.generators import PDE_A, PDE_B, PDE_B_bis, PDE_E, PDE_G, PDE_GS, PDE_N, PDE_Z, RD_Gray_Scott, RD_FitzHugh_Nagumo, RD_RPS, \
    PDE_Laplacian, PDE_O
from ParticleGraph.utils import choose_boundary_values
from ParticleGraph.data_loaders import load_solar_system
from time import sleep
import numpy as np
import torch
from scipy.spatial import Delaunay
from tifffile import imread, imsave
from torch_geometric.utils import get_mesh_laplacian
from tqdm import trange

from ParticleGraph.utils import to_numpy
from torchvision.transforms import v2
from scipy import ndimage


def generate_from_data(config, device, visualize=True, folder=None, step=None):

    data_folder_name = config.data_folder_name

    match data_folder_name:
        case 'graphs_data/solar_system':
            load_solar_system(config, device, visualize, folder, step)
        case _:
            raise ValueError(f'Unknown data folder name {data_folder_name}')


def choose_model(config, device):
    particle_model_name = config.graph_model.particle_model_name
    model_signal_name = config.graph_model.signal_model_name
    aggr_type = config.graph_model.aggr_type
    n_particles = config.simulation.n_particles
    n_node_types = config.simulation.n_node_types
    n_nodes = config.simulation.n_nodes
    n_particle_types = config.simulation.n_particle_types
    bc_pos, bc_dpos = choose_boundary_values(config.simulation.boundary)
    dimension = config.simulation.dimension

    params = config.simulation.params

    match particle_model_name:
        case 'PDE_A' | 'PDE_ParticleField_A' :
            p = torch.ones(n_particle_types, 4, device=device) + torch.rand(n_particle_types, 4, device=device)
            if config.simulation.non_discrete_level>0:
                pp=[]
                n_particle_types = len(params)
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
                for n in range(n_particle_types):
                    if n==0:
                        pp=p[n].repeat(n_particles//n_particle_types,1)
                    else:
                        pp=torch.cat((pp,p[n].repeat(n_particles//n_particle_types,1)),0)
                p=pp.clone().detach()
                p=p+torch.randn(n_particles,4,device=device) * config.simulation.non_discrete_level
            elif params[0] != [-1]:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            else:
                print(p)
            sigma = config.simulation.sigma
            p = p if n_particle_types == 1 else torch.squeeze(p)
            model = PDE_A(aggr_type=aggr_type, p=torch.squeeze(p), sigma=sigma, bc_dpos=bc_dpos, dimension=dimension)
            # matplotlib.use("Qt5Agg")
            # rr = torch.tensor(np.linspace(0, 0.075, 1000)).to(device)
            # for n in range(n_particles):
            #     func= model.psi(rr,p[n])
            #     plt.plot(rr.detach().cpu().numpy(),func.detach().cpu().numpy(),c='k',alpha=0.01)
        case 'PDE_B' | 'PDE_ParticleField_B':
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
        case 'PDE_GS':
            if params[0] == [-1]:
                p = np.linspace(0.5, 5, n_particle_types)
                p = torch.tensor(p, device=device)
            if len(params) > 1:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            model = PDE_GS(aggr_type=aggr_type, p=torch.squeeze(p), clamp=config.training.clamp,
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
            model = PDE_Z(device=device)

    match model_signal_name:

        case 'PDE_N':
            p = torch.rand(n_particle_types, 2, device=device) * 100  # comprised between 10 and 50
            if params[0] != [-1]:
                for n in range(n_particle_types):
                    p[n] = torch.tensor(params[n])
            model = PDE_N(aggr_type=aggr_type, p=torch.squeeze(p), bc_dpos=bc_dpos)



    return model, bc_pos, bc_dpos


def choose_mesh_model(config, device):
    mesh_model_name = config.graph_model.mesh_model_name
    n_node_types = config.simulation.n_node_types
    aggr_type = config.graph_model.mesh_aggr_type
    _, bc_dpos = choose_boundary_values(config.simulation.boundary)

    if mesh_model_name =='':
        mesh_model = []
    else:
        c = initialize_random_values(n_node_types, device)
        if not('pics' in config.simulation.node_type_map):
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
            case 'RD_RPS_Mesh_bis':
                mesh_model = RD_RPS(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
            case 'DiffMesh' | 'WaveMesh':
                mesh_model = PDE_Laplacian(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
            case 'Chemotaxism_Mesh':
                c = initialize_random_values(n_node_types, device)
                for n in range(n_node_types):
                    c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
                mesh_model = PDE_Laplacian(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
            case 'PDE_O_Mesh':
                c = initialize_random_values(n_node_types, device)
                for n in range(n_node_types):
                    c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
                mesh_model = PDE_Laplacian(aggr_type=aggr_type, c=torch.squeeze(c), beta=beta, bc_dpos=bc_dpos)
            case _:
                mesh_model = PDE_Z(device=device)

    return mesh_model


# TODO: this seems to be used to provide default values in case no parameters are given?
def initialize_random_values(n, device):
    return torch.ones(n, 1, device=device) + torch.rand(n, 1, device=device)


def init_particles(config, device):
    simulation_config = config.simulation
    n_particles = simulation_config.n_particles
    n_particle_types = simulation_config.n_particle_types
    dimension = simulation_config.dimension

    dpos_init = simulation_config.dpos_init

    if (simulation_config.boundary == 'periodic'): # | (simulation_config.dimension == 3):
        pos = torch.rand(n_particles, dimension, device=device)
    else:
        pos = torch.randn(n_particles, dimension, device=device) * 0.5
    dpos = dpos_init * torch.randn((n_particles, dimension), device=device)
    dpos = torch.clamp(dpos, min=-torch.std(dpos), max=+torch.std(dpos))
    type = torch.zeros(int(n_particles / n_particle_types), device=device)
    for n in range(1, n_particle_types):
        type = torch.cat((type, n * torch.ones(int(n_particles / n_particle_types), device=device)), 0)
    if (simulation_config.params == 'continuous') | (config.simulation.non_discrete_level > 0):  # TODO: params is a list[list[float]]; this can never happen?
        type = torch.tensor(np.arange(n_particles), device=device)
    features = torch.cat((torch.rand((n_particles, 1), device=device) , 0.1 * torch.randn((n_particles, 1), device=device)), 1)
    type = type[:, None]
    particle_id = torch.arange(n_particles, device=device)
    particle_id = particle_id[:, None]
    age = torch.zeros((n_particles,1), device=device)

    scenario = ''

    match scenario:
        case 'pattern':
            i0 = imread(f'graphs_data/pattern_0.tif')
            type = np.round(i0[(to_numpy(pos[:, 0]) * 255).astype(int), (to_numpy(pos[:, 1]) * 255).astype(int)] / 255 * n_particle_types-1).astype(int)
            type = torch.tensor(type, device=device)
            type = type[:, None]
        case 'uniform':
            type = torch.ones(n_particles, device=device) * 1
            type =  type[:, None]
        case 'stripes':
            l = n_particles//n_particle_types
            for n in range(n_particle_types):
                index = np.arange(n*l, (n+1)*l)
                pos[index, 0:1] = torch.rand(l, 1, device=device) * (1/n_particle_types) + n/n_particle_types
        case _:
            pass

    return pos, dpos, type, features, age, particle_id


def init_cells(config, device):
    simulation_config = config.simulation
    n_particles = simulation_config.n_particles
    n_particle_types = simulation_config.n_particle_types
    dimension = simulation_config.dimension
    has_cell_division = simulation_config.has_cell_division

    dpos_init = simulation_config.dpos_init



    if config.simulation.cell_cycle_length != [-1]:
        cycle_length = torch.tensor(config.simulation.cell_cycle_length, device=device)
    else:
        cycle_length = torch.clamp(torch.abs(torch.ones(n_particle_types, 1, device=device) * 250 + torch.randn(n_particle_types, 1, device=device) * 50), min=100, max=700)
    # 400
    if config.simulation.cell_death_rate != [-1]:
        cell_death_rate = torch.tensor(config.simulation.cell_death_rate, device=device)
    else:
        cell_death_rate = torch.zeros((n_particles, 1), device=device)

    if (simulation_config.boundary == 'periodic'): # | (simulation_config.dimension == 3):
        pos = torch.rand(n_particles, dimension, device=device)
    else:
        pos = torch.randn(n_particles, dimension, device=device) * 0.5
    dpos = dpos_init * torch.randn((n_particles, dimension), device=device)
    dpos = torch.clamp(dpos, min=-torch.std(dpos), max=+torch.std(dpos))
    type = torch.zeros(int(n_particles / n_particle_types), device=device)
    for n in range(1, n_particle_types):
        type = torch.cat((type, n * torch.ones(int(n_particles / n_particle_types), device=device)), 0)
    if (simulation_config.params == 'continuous') | (config.simulation.non_discrete_level > 0):  # TODO: params is a list[list[float]]; this can never happen?
        type = torch.tensor(np.arange(n_particles), device=device)
    features = torch.ones(n_particles, 2, device=device)
    features [:,1] = 0
    cycle_length_distrib = cycle_length[to_numpy(type)].squeeze() * (torch.ones(n_particles, device=device) + 0.05 * torch.randn(n_particles, device=device))
    cycle_duration = torch.rand(n_particles, device=device)
    cycle_duration = cycle_duration * cycle_length[to_numpy(type)].squeeze()
    cycle_duration = cycle_duration[:, None]
    cell_death_rate_distrib = (cell_death_rate[to_numpy(type)].squeeze() * (torch.ones(n_particles, device=device) + 0.05 * torch.randn(n_particles, device=device)))/100
    particle_id = torch.arange(n_particles, device=device)
    particle_id = particle_id[:, None]
    type = type[:, None]

    scenario = ''

    match scenario:
        case 'pattern':
            i0 = imread(f'graphs_data/pattern_0.tif')
            type = np.round(i0[(to_numpy(pos[:, 0]) * 255).astype(int), (to_numpy(pos[:, 1]) * 255).astype(int)] / 255 * n_particle_types-1).astype(int)
            type = torch.tensor(type, device=device)
            type = type[:, None]
        case 'uniform':
            type = torch.ones(n_particles, device=device) * 1
            type =  type[:, None]
        case 'stripes':
            l = n_particles//n_particle_types
            for n in range(n_particle_types):
                index = np.arange(n*l, (n+1)*l)
                pos[index, 0:1] = torch.rand(l, 1, device=device) * (1/n_particle_types) + n/n_particle_types
        case _:
            pass

    return pos, dpos, type, features, cycle_duration, particle_id, cycle_length, cycle_length_distrib, cell_death_rate, cell_death_rate_distrib


def rotate_init_mesh(angle, config, device):
    simulation_config = config.simulation
    n_nodes = simulation_config.n_nodes
    node_value_map = simulation_config.node_value_map

    n_nodes_per_axis = int(np.sqrt(n_nodes))
    xs = torch.linspace(1 / (2 * n_nodes_per_axis), 1 - 1 / (2 * n_nodes_per_axis), steps=n_nodes_per_axis)
    ys = torch.linspace(1 / (2 * n_nodes_per_axis), 1 - 1 / (2 * n_nodes_per_axis), steps=n_nodes_per_axis)
    x_mesh, y_mesh = torch.meshgrid(xs, ys, indexing='xy')
    x_mesh = torch.reshape(x_mesh, (n_nodes_per_axis ** 2, 1))
    y_mesh = torch.reshape(y_mesh, (n_nodes_per_axis ** 2, 1))
    pos_mesh = torch.zeros((n_nodes, 2), device=device)
    pos_mesh[0:n_nodes, 0:1] = x_mesh[0:n_nodes]
    pos_mesh[0:n_nodes, 1:2] = y_mesh[0:n_nodes]


def rotate_init_mesh(angle, config, device):
    simulation_config = config.simulation
    n_nodes = simulation_config.n_nodes
    node_value_map = simulation_config.node_value_map

    n_nodes_per_axis = int(np.sqrt(n_nodes))
    xs = torch.linspace(1 / (2 * n_nodes_per_axis), 1 - 1 / (2 * n_nodes_per_axis), steps=n_nodes_per_axis)
    ys = torch.linspace(1 / (2 * n_nodes_per_axis), 1 - 1 / (2 * n_nodes_per_axis), steps=n_nodes_per_axis)
    x_mesh, y_mesh = torch.meshgrid(xs, ys, indexing='xy')
    x_mesh = torch.reshape(x_mesh, (n_nodes_per_axis ** 2, 1))
    y_mesh = torch.reshape(y_mesh, (n_nodes_per_axis ** 2, 1))
    pos_mesh = torch.zeros((n_nodes, 2), device=device)
    pos_mesh[0:n_nodes, 0:1] = x_mesh[0:n_nodes]
    pos_mesh[0:n_nodes, 1:2] = y_mesh[0:n_nodes]

    i0 = imread(f'graphs_data/{node_value_map}')
    values = i0[(to_numpy(pos_mesh[:, 0]) * 255).astype(int), (to_numpy(pos_mesh[:, 1]) * 255).astype(int)]
    values = np.reshape(values, (n_nodes_per_axis, n_nodes_per_axis))
    values = ndimage.rotate(values, angle, reshape=False, cval=np.mean(values)*1.1)
    values = np.reshape(values, (n_nodes_per_axis*n_nodes_per_axis))
    features_mesh = torch.zeros((n_nodes, 2), device=device)
    features_mesh[:, 0] = torch.tensor(values / 255 * 5000, device=device)

    return features_mesh


def get_index(n_particles, n_particle_types):
    index_particles = []
    for n in range(n_particle_types):
        index_particles.append(
            np.arange((n_particles // n_particle_types) * n, (n_particles // n_particle_types) * (n + 1)))
    return index_particles


def init_mesh(config, model_mesh, device):
    simulation_config = config.simulation
    n_nodes = simulation_config.n_nodes
    n_particles = simulation_config.n_particles
    node_value_map = simulation_config.node_value_map
    node_type_map = simulation_config.node_type_map

    n_nodes_per_axis = int(np.sqrt(n_nodes))
    xs = torch.linspace(1 / (2 * n_nodes_per_axis), 1 - 1 / (2 * n_nodes_per_axis), steps=n_nodes_per_axis)
    ys = torch.linspace(1 / (2 * n_nodes_per_axis), 1 - 1 / (2 * n_nodes_per_axis), steps=n_nodes_per_axis)
    x_mesh, y_mesh = torch.meshgrid(xs, ys, indexing='xy')
    x_mesh = torch.reshape(x_mesh, (n_nodes_per_axis ** 2, 1))
    y_mesh = torch.reshape(y_mesh, (n_nodes_per_axis ** 2, 1))
    mesh_size = 1 / n_nodes_per_axis
    pos_mesh = torch.zeros((n_nodes, 2), device=device)
    pos_mesh[0:n_nodes, 0:1] = x_mesh[0:n_nodes]
    pos_mesh[0:n_nodes, 1:2] = y_mesh[0:n_nodes]

    i0 = imread(f'graphs_data/{node_value_map}')
    if 'video' in simulation_config.node_value_map:
        i0 = imread(f'graphs_data/pattern_Null.tif')
    else:
        i0 = imread(f'graphs_data/{node_value_map}')
    values = i0[(to_numpy(pos_mesh[:, 1]) * 255).astype(int), (to_numpy(pos_mesh[:, 0]) * 255).astype(int)]

    mask_mesh = (x_mesh > torch.min(x_mesh) + 0.02) & (x_mesh < torch.max(x_mesh) - 0.02) & (y_mesh > torch.min(y_mesh) + 0.02) & (y_mesh < torch.max(y_mesh) - 0.02)

    pos_mesh = pos_mesh + torch.randn(n_nodes, 2, device=device) * mesh_size / 8

    match config.graph_model.mesh_model_name:
        case 'RD_Gray_Scott_Mesh':
            features_mesh = torch.zeros((n_nodes, 2), device=device)
            features_mesh[:, 0] -= 0.5 * torch.tensor(values / 255, device=device)
            features_mesh[:, 1] = 0.25 * torch.tensor(values / 255, device=device)
        case 'RD_FitzHugh_Nagumo_Mesh':
            features_mesh = torch.zeros((n_nodes, 2), device=device) + torch.rand((n_nodes, 2), device=device) * 0.1
        case 'RD_RPS_Mesh' | 'RD_RPS_Mesh_bis':
            features_mesh = torch.rand((n_nodes, 3), device=device)
            s = torch.sum(features_mesh, dim=1)
            for k in range(3):
                features_mesh[:, k] = features_mesh[:, k] / s
        case '' | 'DiffMesh' | 'WaveMesh' | 'Particle_Mesh_A' | 'Particle_Mesh_B':
            features_mesh = torch.zeros((n_nodes, 2), device=device)
            features_mesh[:, 0] = torch.tensor(values / 255 * 5000, device=device)
        case 'PDE_O_Mesh':
            features_mesh = torch.zeros((n_particles, 5), device=device)
            features_mesh[0:n_particles, 0:1] = x_mesh[0:n_particles]
            features_mesh[0:n_particles, 1:2] = y_mesh[0:n_particles]
            features_mesh[0:n_particles, 2:3] = torch.randn(n_particles, 1, device=device) * 2 * np.pi  # theta
            features_mesh[0:n_particles, 3:4] = torch.ones(n_particles, 1, device=device) * np.pi / 200  # d_theta
            features_mesh[0:n_particles, 4:5] = features_mesh[0:n_particles, 3:4]  # d_theta0
            pos_mesh[:, 0] = features_mesh[:, 0] + (3 / 8) * mesh_size * torch.cos(features_mesh[:, 2])
            pos_mesh[:, 1] = features_mesh[:, 1] + (3 / 8) * mesh_size * torch.sin(features_mesh[:, 2])

    # i0 = imread(f'graphs_data/{node_type_map}')
    # values = i0[(to_numpy(x_mesh[:, 0]) * 255).astype(int), (to_numpy(y_mesh[:, 0]) * 255).astype(int)]
    # type_mesh = torch.tensor(values, device=device)
    # type_mesh = type_mesh[:, None]

    i0 = imread(f'graphs_data/{node_type_map}')
    values = i0[(to_numpy(x_mesh[:, 0]) * 255).astype(int), (to_numpy(y_mesh[:, 0]) * 255).astype(int)]
    if np.max(values) > 0:
        values = np.round(values / np.max(values) * (simulation_config.n_node_types-1))
    type_mesh = torch.tensor(values, device=device)
    type_mesh = type_mesh[:, None]

    node_id_mesh = torch.arange(n_nodes, device=device)
    node_id_mesh = node_id_mesh[:, None]
    dpos_mesh = torch.zeros((n_nodes, 2), device=device)

    x_mesh = torch.concatenate((node_id_mesh.clone().detach(), pos_mesh.clone().detach(), dpos_mesh.clone().detach(),
                                type_mesh.clone().detach(), features_mesh.clone().detach()), 1)

    pos = to_numpy(x_mesh[:, 1:3])
    tri = Delaunay(pos, qhull_options='QJ')
    face = torch.from_numpy(tri.simplices)
    face_longest_edge = np.zeros((face.shape[0], 1))

    print('Removal of skinny faces ...')
    sleep(0.5)
    for k in trange(face.shape[0]):
        # compute edge distances
        x1 = pos[face[k, 0], :]
        x2 = pos[face[k, 1], :]
        x3 = pos[face[k, 2], :]
        a = np.sqrt(np.sum((x1 - x2) ** 2))
        b = np.sqrt(np.sum((x2 - x3) ** 2))
        c = np.sqrt(np.sum((x3 - x1) ** 2))
        A = np.max([a, b]) / np.min([a, b])
        B = np.max([a, c]) / np.min([a, c])
        C = np.max([c, b]) / np.min([c, b])
        face_longest_edge[k] = np.max([A, B, C])

    face_kept = np.argwhere(face_longest_edge < 5)
    face_kept = face_kept[:, 0]
    face = face[face_kept, :]
    face = face.t().contiguous()
    face = face.to(device, torch.long)

    pos_3d = torch.cat((x_mesh[:, 1:3], torch.ones((x_mesh.shape[0], 1), device=device)), dim=1)
    edge_index_mesh, edge_weight_mesh = get_mesh_laplacian(pos=pos_3d, face=face, normalization="None")
    edge_weight_mesh = edge_weight_mesh.to(dtype=torch.float32)
    mesh_data = {'mesh_pos': pos_3d, 'face': face, 'edge_index': edge_index_mesh, 'edge_weight': edge_weight_mesh,
                 'mask': mask_mesh, 'size': mesh_size}

    if (config.graph_model.particle_model_name == 'PDE_ParticleField_A')  | (config.graph_model.particle_model_name == 'PDE_ParticleField_B'):

        type_mesh = 0 * type_mesh

    # if config.graph_model.particle_model_name == 'PDE_ParticleField_B':
    #
    #     a1 = 1E-2  # diffusion coefficient
    #     a2 = 8E-5  # positive rate coefficient
    #     a3 = 6.65E-5  # negative rate coefficient
    #
    #     i0 = imread(f'graphs_data/{config.simulation.node_diffusion_map}')
    #     index = np.round(
    #         i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(int)]).astype(int)
    #     coeff_diff = a1 * np.array(config.simulation.diffusion_coefficients)[index]
    #     model.coeff_diff = torch.tensor(coeff_diff, device=device)
    #     i0 = imread(f'graphs_data/{config.simulation.node_proliferation_map}')
    #     index = np.round(
    #         i0[(to_numpy(x_mesh[:, 0]) * 255).astype(int), (to_numpy(x_mesh[:, 1]) * 255).astype(int)]).astype(int)
    #     pos_rate = a2 * np.array(config.simulation.pos_rate)[index]
    #     model.pos_rate = torch.tensor(pos_rate, device=device)
    #     model.neg_rate = - torch.ones_like(model.pos_rate) * a3 * torch.tensor(config.simulation.pos_rate[0], device=device)
    #
    #     type_mesh = -1.0 + type_mesh * -1.0

    a_mesh = torch.zeros_like(type_mesh)
    type_mesh = type_mesh.to(dtype=torch.float32)


    return pos_mesh, dpos_mesh, type_mesh, features_mesh, a_mesh, node_id_mesh, mesh_data