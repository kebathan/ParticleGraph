import matplotlib.cm as cmplt
import matplotlib.pyplot as plt
import numpy as np
import torch
from matplotlib.ticker import FormatStrFormatter
from torch_geometric.nn import MessagePassing
import torch_geometric.utils as pyg_utils
import os
from ParticleGraph.MLP import MLP
import imageio
from matplotlib import rc
import time
from ParticleGraph.utils import *
from ParticleGraph.fitting_models import *
from ParticleGraph.kan import *

os.environ["PATH"] += os.pathsep + '/usr/local/texlive/2023/bin/x86_64-linux'

# from data_loaders import *

from GNN_particles_Ntype import *
from ParticleGraph.embedding_cluster import *
from ParticleGraph.utils import to_numpy, CustomColorMap, choose_boundary_values
import matplotlib as mpl
from matplotlib.ticker import FuncFormatter
from io import StringIO
import sys


# matplotlib.use("Qt5Agg")

class Interaction_Particles_extract(MessagePassing):
    """Interaction Network as proposed in this paper:
    https://proceedings.neurips.cc/paper/2016/hash/3147da8ab4a0437c15ef51a5cc7f2dc4-Abstract.html"""

    def __init__(self, config, device, aggr_type=None, bc_dpos=None):

        super(Interaction_Particles_extract, self).__init__(aggr=aggr_type)  # "Add" aggregation.

        config.simulation = config.simulation
        config.graph_model = config.graph_model
        config.training = config.training

        self.device = device
        self.input_size = config.graph_model.input_size
        self.output_size = config.graph_model.output_size
        self.hidden_dim = config.graph_model.hidden_dim
        self.n_layers = config.graph_model.n_mp_layers
        self.n_particles = config.simulation.n_particles
        self.max_radius = config.simulation.max_radius
        self.data_augmentation = config.training.data_augmentation
        self.noise_level = config.training.noise_level
        self.embedding_dim = config.graph_model.embedding_dim
        self.n_dataset = config.training.n_runs
        self.prediction = config.graph_model.prediction
        self.update_type = config.graph_model.update_type
        self.n_layers_update = config.graph_model.n_layers_update
        self.hidden_dim_update = config.graph_model.hidden_dim_update
        self.sigma = config.simulation.sigma
        self.model = config.graph_model.particle_model_name
        self.bc_dpos = bc_dpos
        self.n_ghosts = int(config.training.n_ghosts)
        self.n_particles_max = config.simulation.n_particles_max

        self.lin_edge = MLP(input_size=self.input_size, output_size=self.output_size, nlayers=self.n_layers,
                            hidden_size=self.hidden_dim, device=self.device)

        if config.simulation.has_cell_division:
            self.a = nn.Parameter(
                torch.tensor(np.ones((self.n_dataset, self.n_particles_max, 2)), device=self.device,
                             requires_grad=True, dtype=torch.float32))
        else:
            self.a = nn.Parameter(
                torch.tensor(np.ones((self.n_dataset, int(self.n_particles) + self.n_ghosts, self.embedding_dim)),
                             device=self.device,
                             requires_grad=True, dtype=torch.float32))

        if self.update_type != 'none':
            self.lin_update = MLP(input_size=self.output_size + self.embedding_dim + 2, output_size=self.output_size,
                                  nlayers=self.n_layers_update, hidden_size=self.hidden_dim_update, device=self.device)

    def forward(self, data=[], data_id=[], training=[], vnorm=[], phi=[], has_field=False):

        self.data_id = data_id
        self.vnorm = vnorm
        self.cos_phi = torch.cos(phi)
        self.sin_phi = torch.sin(phi)
        self.training = training
        self.has_field = has_field

        x, edge_index = data.x, data.edge_index
        edge_index, _ = pyg_utils.remove_self_loops(edge_index)

        pos = x[:, 1:3]
        d_pos = x[:, 3:5]
        particle_id = x[:, 0:1]
        if has_field:
            field = x[:, 6:7]
        else:
            field = torch.ones_like(x[:, 6:7])

        pred = self.propagate(edge_index, pos=pos, d_pos=d_pos, particle_id=particle_id, field=field)

        return pred, self.in_features, self.lin_edge_out

    def message(self, pos_i, pos_j, d_pos_i, d_pos_j, particle_id_i, particle_id_j, field_j):
        # squared distance
        r = torch.sqrt(torch.sum(self.bc_dpos(pos_j - pos_i) ** 2, dim=1)) / self.max_radius
        delta_pos = self.bc_dpos(pos_j - pos_i) / self.max_radius
        dpos_x_i = d_pos_i[:, 0] / self.vnorm
        dpos_y_i = d_pos_i[:, 1] / self.vnorm
        dpos_x_j = d_pos_j[:, 0] / self.vnorm
        dpos_y_j = d_pos_j[:, 1] / self.vnorm

        if self.data_augmentation & (self.training == True):
            new_delta_pos_x = self.cos_phi * delta_pos[:, 0] + self.sin_phi * delta_pos[:, 1]
            new_delta_pos_y = -self.sin_phi * delta_pos[:, 0] + self.cos_phi * delta_pos[:, 1]
            delta_pos[:, 0] = new_delta_pos_x
            delta_pos[:, 1] = new_delta_pos_y
            new_dpos_x_i = self.cos_phi * dpos_x_i + self.sin_phi * dpos_y_i
            new_dpos_y_i = -self.sin_phi * dpos_x_i + self.cos_phi * dpos_y_i
            dpos_x_i = new_dpos_x_i
            dpos_y_i = new_dpos_y_i
            new_dpos_x_j = self.cos_phi * dpos_x_j + self.sin_phi * dpos_y_j
            new_dpos_y_j = -self.sin_phi * dpos_x_j + self.cos_phi * dpos_y_j
            dpos_x_j = new_dpos_x_j
            dpos_y_j = new_dpos_y_j

        embedding_i = self.a[self.data_id, to_numpy(particle_id_i), :].squeeze()
        embedding_j = self.a[self.data_id, to_numpy(particle_id_j), :].squeeze()

        match self.model:
            case 'PDE_A':
                in_features = torch.cat((delta_pos, r[:, None], embedding_i), dim=-1)
            case 'PDE_B' | 'PDE_B_bis':
                in_features = torch.cat((delta_pos, r[:, None], dpos_x_i[:, None], dpos_y_i[:, None], dpos_x_j[:, None],
                                         dpos_y_j[:, None], embedding_i), dim=-1)
            case 'PDE_G':
                in_features = torch.cat((delta_pos, r[:, None], dpos_x_i[:, None], dpos_y_i[:, None],
                                         dpos_x_j[:, None], dpos_y_j[:, None], embedding_j), dim=-1)
            case 'PDE_GS':
                in_features = torch.cat((r[:, None], embedding_j), dim=-1)
            case 'PDE_E':
                in_features = torch.cat(
                    (delta_pos, r[:, None], embedding_i, embedding_j), dim=-1)

        out = self.lin_edge(in_features) * field_j

        self.in_features = in_features
        self.lin_edge_out = out

        return out

    def update(self, aggr_out):

        return aggr_out  # self.lin_node(aggr_out)

    def psi(self, r, p):

        if (len(p) == 3):  # PDE_B
            cohesion = p[0] * 0.5E-5 * r
            separation = -p[2] * 1E-8 / r
            return (cohesion + separation) * p[1] / 500  #
        else:  # PDE_A
            return r * (p[0] * torch.exp(-r ** (2 * p[1]) / (2 * self.sigma ** 2)) - p[2] * torch.exp(
                -r ** (2 * p[3]) / (2 * self.sigma ** 2)))


class PDE_B_extract(MessagePassing):
    """Interaction Network as proposed in this paper:
    https://proceedings.neurips.cc/paper/2016/hash/3147da8ab4a0437c15ef51a5cc7f2dc4-Abstract.html"""

    def __init__(self, aggr_type=None, p=None, bc_dpos=None):
        super(PDE_B_extract, self).__init__(aggr=aggr_type)  # "mean" aggregation.

        self.p = p
        self.bc_dpos = bc_dpos

        self.a1 = 0.5E-5
        self.a2 = 5E-4
        self.a3 = 1E-8

    def forward(self, data):
        x, edge_index = data.x, data.edge_index
        edge_index, _ = pyg_utils.remove_self_loops(edge_index)
        acc = self.propagate(edge_index, x=x)

        sum = self.cohesion + self.alignment + self.separation

        return acc, sum, self.cohesion, self.alignment, self.separation, self.diffx, self.diffv, self.r, self.type

    def message(self, x_i, x_j):
        r = torch.sum(self.bc_dpos(x_j[:, 1:3] - x_i[:, 1:3]) ** 2, dim=1)  # distance squared

        pp = self.p[to_numpy(x_i[:, 5]), :]

        cohesion = pp[:, 0:1].repeat(1, 2) * self.a1 * self.bc_dpos(x_j[:, 1:3] - x_i[:, 1:3])
        alignment = pp[:, 1:2].repeat(1, 2) * self.a2 * self.bc_dpos(x_j[:, 3:5] - x_i[:, 3:5])
        separation = pp[:, 2:3].repeat(1, 2) * self.a3 * self.bc_dpos(x_i[:, 1:3] - x_j[:, 1:3]) / (
            r[:, None].repeat(1, 2))

        self.cohesion = cohesion
        self.alignment = alignment
        self.separation = separation

        self.r = r
        self.diffx = self.bc_dpos(x_j[:, 1:3] - x_i[:, 1:3])
        self.diffv = self.bc_dpos(x_j[:, 3:5] - x_i[:, 3:5])
        self.type = x_i[:, 5]

        return (separation + alignment + cohesion)

    def psi(self, r, p):
        cohesion = p[0] * self.a1 * r
        separation = -p[2] * self.a3 / r
        return (cohesion + separation)


class Mesh_RPS_extract(MessagePassing):
    """Interaction Network as proposed in this paper:
    https://proceedings.neurips.cc/paper/2016/hash/3147da8ab4a0437c15ef51a5cc7f2dc4-Abstract.html"""

    def __init__(self, aggr_type=None, config=None, device=None, bc_dpos=None):
        super(Mesh_RPS_extract, self).__init__(aggr=aggr_type)

        config.simulation = config.simulation
        config.graph_model = config.graph_model

        self.device = device
        self.input_size = config.graph_model.input_size
        self.output_size = config.graph_model.output_size
        self.hidden_size = config.graph_model.hidden_dim
        self.nlayers = config.graph_model.n_mp_layers
        self.embedding_dim = config.graph_model.embedding_dim
        self.nparticles = config.simulation.n_particles
        self.ndataset = config.training.n_runs
        self.bc_dpos = bc_dpos

        self.lin_phi = MLP(input_size=self.input_size, output_size=self.output_size, nlayers=self.nlayers,
                           hidden_size=self.hidden_size, device=self.device)

        self.a = nn.Parameter(
            torch.tensor(np.ones((int(self.ndataset), int(self.nparticles), self.embedding_dim)), device=self.device,
                         requires_grad=True, dtype=torch.float32))

    def forward(self, data, data_id):
        self.data_id = data_id
        x, edge_index, edge_attr = data.x, data.edge_index, data.edge_attr

        uvw = data.x[:, 6:9]

        laplacian_uvw = self.propagate(edge_index, uvw=uvw, discrete_laplacian=edge_attr)

        particle_id = to_numpy(x[:, 0])
        embedding = self.a[self.data_id, particle_id, :]

        input_phi = torch.cat((laplacian_uvw, uvw, embedding), dim=-1)

        pred = self.lin_phi(input_phi)

        return pred, input_phi, embedding

    def message(self, uvw_j, discrete_laplacian):
        return discrete_laplacian[:, None] * uvw_j

    def update(self, aggr_out):
        return aggr_out  # self.lin_node(aggr_out)

    def psi(self, r, p):
        return p * r

    def plot_embedding_func_cluster(model, config, config_file, embedding_cluster, cmap, index_particles,
                                    n_particle_types, n_particles, ynorm, epoch, log_dir, device):

        embedding = get_embedding(model.a, 1)

        func_list, proj_interaction = analyze_edge_function(rr=[], vizualize=False, config=config,
                                                            model_lin_edge=model.lin_edge, model_a=model.a,
                                                            dataset_number=1,
                                                            n_particles=n_particles, ynorm=ynorm,
                                                            types=to_numpy(x[:, 5]),
                                                            cmap=cmap, device=device)

        labels, n_clusters, new_labels = sparsify_cluster(config.training.cluster_method, proj_interaction, embedding,
                                                          config.training.cluster_distance_threshold, index_particles,
                                                          n_particle_types, embedding_cluster)

        accuracy = metrics.accuracy_score(to_numpy(type_list), new_labels)

        model_a_ = model.a[1].clone().detach()
        for n in range(n_clusters):
            pos = np.argwhere(labels == n).squeeze().astype(int)
            pos = np.array(pos)
            if pos.size > 0:
                median_center = model_a_[pos, :]
                median_center = torch.median(median_center, dim=0).values

        model_a_first = model.a.clone().detach()

        with torch.no_grad():
            model.a[1] = model_a_.clone().detach()

        fig, ax = fig_init()
        embedding = get_embedding(model_a_first, 1)
        csv_ = embedding
        np.save(f"./{log_dir}/results/embedding_{config_file}_{epoch}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/embedding_{config_file}_{epoch}.txt", csv_)
        if n_particle_types > 1000:
            plt.scatter(embedding[:, 0], embedding[:, 1], c=to_numpy(x[:, 5]) / n_particles, s=10,
                        cmap=cc)
        else:
            for n in range(n_particle_types):
                plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], color=cmap.color(n),
                            s=400, alpha=0.1)
        plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()


def load_training_data(dataset_name, n_runs, log_dir, device):
    x_list = []
    y_list = []
    print('Load data ...')
    time.sleep(0.5)
    for run in trange(n_runs):
        x = torch.load(f'graphs_data/graphs_{dataset_name}/x_list_{run}.pt', map_location=device)
        y = torch.load(f'graphs_data/graphs_{dataset_name}/y_list_{run}.pt', map_location=device)
        x_list.append(x)
        y_list.append(y)
    vnorm = torch.load(os.path.join(log_dir, 'vnorm.pt'), map_location=device).squeeze()
    ynorm = torch.load(os.path.join(log_dir, 'ynorm.pt'), map_location=device).squeeze()
    print("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    x = []
    y = []

    return x_list, y_list, vnorm, ynorm


def plot_embedding_func_cluster_tracking(model, config, config_file, embedding_cluster, cmap, index_particles, indexes, type_list,
                                n_particle_types, n_particles, ynorm, epoch, log_dir, embedding_type, device):

    print('analyze GNN ...')

    if embedding_type == 1:
        embedding = to_numpy(model.a.clone().detach())
        fig, ax = fig_init()
        for n, k in enumerate(indexes):
            plt.scatter(embedding[int(k), 0], embedding[int(k), 1], s=1, color=cmap.color(int(type_list[int(n)])), alpha=0.25)
        plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.xlim([-40, 40])
        plt.ylim([-40, 40])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/all_embedding_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()
    else:
        fig, ax = fig_init()
        for k in trange(0, config.simulation.n_frames - 2):
            embedding = to_numpy(model.a[k * n_particles:(k + 1) * n_particles, :].clone().detach())
            for n in range(n_particle_types):
                plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], s=1,
                            color=cmap.color(n), alpha=0.025)
        plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.xlim([-40, 40])
        plt.ylim([-40, 40])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/all_embedding_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()

    func_list, proj_interaction = analyze_edge_function_tracking(rr=[], vizualize=False, config=config,
                                                        model_lin_edge=model.lin_edge, model_a=model.a,
                                                        n_particles=n_particles, ynorm=ynorm,
                                                        indexes=indexes, type_list = type_list,
                                                        cmap=cmap, embedding_type = embedding_type, device=device)

    fig, ax = fig_init()
    proj_interaction = (proj_interaction - np.min(proj_interaction)) / (
                np.max(proj_interaction) - np.min(proj_interaction) + 1e-10)
    if embedding_type == 1:
        for n, k in enumerate(indexes):
            plt.scatter(proj_interaction[int(n), 0], proj_interaction[int(n), 1], s=1, color=cmap.color(int(type_list[int(n)])), alpha=0.25)
    else:
        for n in range(n_particle_types):
            plt.scatter(proj_interaction[index_particles[n], 0],
                        proj_interaction[index_particles[n], 1], color=cmap.color(n), s=200, alpha=0.1)
    plt.xlabel(r'UMAP 0', fontsize=64)
    plt.ylabel(r'UMAP 1', fontsize=64)
    plt.xlim([-0.2, 1.2])
    plt.ylim([-0.2, 1.2])
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/UMAP_{config_file}_{epoch}.tif", dpi=170.7)
    plt.close()

    labels, n_clusters, new_labels = sparsify_cluster(config.training.cluster_method, proj_interaction, embedding,
                                                      config.training.cluster_distance_threshold, index_particles,
                                                      n_particle_types, embedding_cluster)

    accuracy = metrics.accuracy_score(type_list, new_labels)

    return accuracy, n_clusters, new_labels




def plot_embedding_func_cluster(model, config, config_file, embedding_cluster, cmap, index_particles, indexes,
                                n_particle_types, n_particles, ynorm, epoch, log_dir, device):

    print('analyze GNN ...')

    fig, ax = fig_init()
    if config.training.has_no_tracking:
        embedding = to_numpy(model.a[0:n_particles])
    else:
        embedding = get_embedding(model.a, 1)
    if n_particle_types > 1000:
        plt.scatter(embedding[:, 0], embedding[:, 1], c=to_numpy(x[:, 5]) / n_particles, s=10,
                    cmap=cc)
    else:
        for n in range(n_particle_types):
            plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], color=cmap.color(n),
                        s=400, alpha=0.1)
    plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
    plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/first_embedding_{config_file}_{epoch}.tif", dpi=170.7)
    plt.close()

    func_list, proj_interaction = analyze_edge_function(rr=[], vizualize=False, config=config,
                                                        model_lin_edge=model.lin_edge, model_a=model.a,
                                                        dataset_number=1,
                                                        n_particles=n_particles, ynorm=ynorm,
                                                        types=to_numpy(type_list),
                                                        cmap=cmap, device=device)

    fig, ax = fig_init()
    proj_interaction = (proj_interaction - np.min(proj_interaction)) / (
                np.max(proj_interaction) - np.min(proj_interaction) + 1e-10)
    for n in range(n_particle_types):
        plt.scatter(proj_interaction[index_particles[n], 0],
                    proj_interaction[index_particles[n], 1], color=cmap.color(n), s=200, alpha=0.1)
    plt.xlabel(r'UMAP 0', fontsize=64)
    plt.ylabel(r'UMAP 1', fontsize=64)
    plt.xlim([-0.2, 1.2])
    plt.ylim([-0.2, 1.2])
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/UMAP_{config_file}_{epoch}.tif", dpi=170.7)
    plt.close()

    labels, n_clusters, new_labels = sparsify_cluster(config.training.cluster_method, proj_interaction, embedding,
                                                      config.training.cluster_distance_threshold, index_particles,
                                                      n_particle_types, embedding_cluster)

    accuracy = metrics.accuracy_score(to_numpy(type_list), new_labels)

    model_a_ = model.a[1].clone().detach()
    for n in range(n_clusters):
        pos = np.argwhere(labels == n).squeeze().astype(int)
        pos = np.array(pos)
        if pos.size > 0:
            median_center = model_a_[pos, :]
            median_center = torch.median(median_center, dim=0).values
            model_a_[pos, :] = median_center
    with torch.no_grad():
        model.a[1] = model_a_.clone().detach()

    fig, ax = fig_init()
    embedding = get_embedding(model.a, 1)
    if n_particle_types > 1000:
        plt.scatter(embedding[:, 0], embedding[:, 1], c=to_numpy(x[:, 5]) / n_particles, s=10,
                    cmap=cc)
    else:
        for n in range(n_particle_types):
            plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], color=cmap.color(n),
                        s=400, alpha=0.1)
    plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
    plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{epoch}.tif", dpi=170.7)
    plt.close()

    return accuracy, n_clusters, new_labels


def plot_embedding(index, model_a, dataset_number, index_particles, n_particles, n_particle_types, epoch, it, fig, ax,
                   cmap, device):
    embedding = get_embedding(model_a, dataset_number)

    plt.text(-0.25, 1.1, f'{index}', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    plt.title(r'Particle embedding', fontsize=12)
    for n in range(n_particle_types):
        plt.scatter(embedding[index_particles[n], 0],
                    embedding[index_particles[n], 1], color=cmap.color(n), s=0.1)
    plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=12)
    plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=12)
    plt.text(.05, .94, f'e: {epoch} it: {it}', ha='left', va='top', transform=ax.transAxes, fontsize=10)
    plt.text(.05, .86, f'N: {n_particles}', ha='left', va='top', transform=ax.transAxes, fontsize=10)
    plt.xticks(fontsize=10.0)
    plt.yticks(fontsize=10.0)

    return embedding


def plot_function(bVisu, index, model_name, model_MLP, model_a, dataset_number, label, pos, max_radius, ynorm,
                  index_particles, n_particles, n_particle_types, epoch, it, fig, ax, cmap, device):
    # print(f'plot functions epoch:{epoch} it: {it}')

    plt.text(-0.25, 1.1, f'{index}', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    plt.title(r'Interaction functions (model)', fontsize=12)
    func_list = []
    for n in range(n_particles):
        embedding_ = model_a[1, n, :] * torch.ones((1000, 2), device=device)

        match model_name:
            case 'PDE_A':
                in_features = torch.cat((pos[:, None] / max_radius, 0 * pos[:, None],
                                         pos[:, None] / max_radius, embedding_), dim=1)
            case 'PDE_A_bis':
                in_features = torch.cat((pos[:, None] / max_radius, 0 * pos[:, None],
                                         pos[:, None] / max_radius, embedding_, embedding_), dim=1)
            case 'PDE_B' | 'PDE_B_bis':
                in_features = torch.cat((pos[:, None] / max_radius, 0 * pos[:, None],
                                         pos[:, None] / max_radius, 0 * pos[:, None], 0 * pos[:, None],
                                         0 * pos[:, None], 0 * pos[:, None], embedding_), dim=1)
            case 'PDE_G':
                in_features = torch.cat((pos[:, None] / max_radius, 0 * pos[:, None],
                                         pos[:, None] / max_radius, 0 * pos[:, None], 0 * pos[:, None],
                                         0 * pos[:, None], 0 * pos[:, None], embedding_), dim=1)
            case 'PDE_GS':
                in_features = torch.cat((pos[:, None] / max_radius, embedding_), dim=1)
            case 'PDE_E':
                in_features = torch.cat((pos[:, None] / max_radius, 0 * pos[:, None],
                                         pos[:, None] / max_radius, embedding_, embedding_), dim=-1)

        with torch.no_grad():
            func = model_MLP(in_features.float())
        func = func[:, 0]
        func_list.append(func)
        if bVisu:
            plt.plot(to_numpy(pos),
                     to_numpy(func) * to_numpy(ynorm), color=cmap.color(label[n]), linewidth=1)
    func_list = torch.stack(func_list)
    func_list = to_numpy(func_list)
    if bVisu:
        plt.xlabel(r'$d_{ij} [a.u.]$', fontsize=12)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij} [a.u.]$', fontsize=12)
        plt.xticks(fontsize=10.0)
        plt.yticks(fontsize=10.0)
        # plt.ylim([-0.04, 0.03])
        plt.text(.05, .86, f'N: {n_particles // 50}', ha='left', va='top', transform=ax.transAxes, fontsize=10)
        plt.text(.05, .94, f'e: {epoch} it: {it}', ha='left', va='top', transform=ax.transAxes, fontsize=10)

    return func_list


def plot_umap(index, func_list, log_dir, n_neighbors, index_particles, n_particles, n_particle_types, embedding_cluster,
              epoch, it, fig, ax, cmap, device):
    # print(f'plot umap epoch:{epoch} it: {it}')
    plt.text(-0.25, 1.1, f'{index}', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    if False:  # os.path.exists(os.path.join(log_dir, f'proj_interaction_{epoch}.npy')):
        proj_interaction = np.load(os.path.join(log_dir, f'proj_interaction_{epoch}.npy'))
    else:
        new_index = np.random.permutation(func_list.shape[0])
        new_index = new_index[0:min(1000, func_list.shape[0])]
        trans = umap.UMAP(n_neighbors=n_neighbors, n_components=2, transform_queue_size=0).fit(func_list[new_index])
        proj_interaction = trans.transform(func_list)
    np.save(os.path.join(log_dir, f'proj_interaction_{epoch}.npy'), proj_interaction)
    plt.title(r'UMAP of $f(\ensuremath{\mathbf{a}}_i, d_{ij}$)', fontsize=12)

    labels, n_clusters = embedding_cluster.get(proj_interaction, 'distance')

    label_list = []
    for n in range(n_particle_types):
        tmp = labels[index_particles[n]]
        label_list.append(np.round(np.median(tmp)))
    label_list = np.array(label_list)
    new_labels = labels.copy()
    for n in range(n_particle_types):
        new_labels[labels == label_list[n]] = n
        plt.scatter(proj_interaction[index_particles[n], 0], proj_interaction[index_particles[n], 1],
                    color=cmap.color(n), s=1)

    plt.xlabel(r'UMAP 0', fontsize=12)
    plt.ylabel(r'UMAP 1', fontsize=12)

    plt.xticks(fontsize=10.0)
    plt.yticks(fontsize=10.0)
    plt.text(.05, .86, f'N: {n_particles}', ha='left', va='top', transform=ax.transAxes, fontsize=10)
    plt.text(.05, .94, f'e: {epoch} it: {it}', ha='left', va='top', transform=ax.transAxes, fontsize=10)

    return proj_interaction, new_labels, n_clusters


def plot_confusion_matrix(index, true_labels, new_labels, n_particle_types, epoch, it, fig, ax):
    # print(f'plot confusion matrix epoch:{epoch} it: {it}')
    plt.text(-0.25, 1.1, f'{index}', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    confusion_matrix = metrics.confusion_matrix(true_labels, new_labels)  # , normalize='true')
    cm_display = metrics.ConfusionMatrixDisplay(confusion_matrix=confusion_matrix)
    if n_particle_types > 8:
        cm_display.plot(ax=fig.gca(), cmap='Blues', include_values=False, colorbar=False)
    else:
        cm_display.plot(ax=fig.gca(), cmap='Blues', include_values=True, values_format='d', colorbar=False)
    accuracy = metrics.accuracy_score(true_labels, new_labels)
    plt.title(f'accuracy: {np.round(accuracy, 2)}', fontsize=12)
    # print(f'accuracy: {np.round(accuracy,3)}')
    plt.xticks(fontsize=10.0)
    plt.yticks(fontsize=10.0)
    plt.xlabel(r'Predicted label', fontsize=12)
    plt.ylabel(r'True label', fontsize=12)

    return accuracy


def plot_cell_rates(config, device, log_dir, n_particle_types, x_list, new_labels, cmap, logger):

    n_frames = config.simulation.n_frames
    cell_cycle_length = np.array(config.simulation.cell_cycle_length)

    print('plot cell rates ...')
    N_cells_alive = np.zeros((n_frames, n_particle_types))
    N_cells_dead = np.zeros((n_frames, n_particle_types))

    if os.path.exists(f"./{log_dir}/results/x_.npy"):
        x_ = np.load(f"./{log_dir}/results/x_.npy")
        N_cells_alive = np.load(f"./{log_dir}/results/cell_alive.npy")
        N_cells_dead = np.load(f"./{log_dir}/results/cell_dead.npy")
    else:
        for it in trange(n_frames):

            x = x_list[0][it].clone().detach()
            particle_index = to_numpy(x[:, 0:1]).astype(int)
            x[:, 5:6] = torch.tensor(new_labels[particle_index], device=device)
            if it == 0:
                x_=x_list[0][it].clone().detach()
            else:
                x_=torch.concatenate((x_,x),axis=0)

            for k in range(n_particle_types):
                pos = torch.argwhere((x[:, 5:6] == k) & (x[:, 6:7] == 1))
                N_cells_alive[it, k] = pos.shape[0]
                pos = torch.argwhere((x[:, 5:6] == k) & (x[:, 6:7] == 0))
                N_cells_dead[it, k] = pos.shape[0]

        x_list=[]
        x_ = to_numpy(x_)

        print('save data ...')

        np.save(f"./{log_dir}/results/cell_alive.npy", N_cells_alive)
        np.save(f"./{log_dir}/results/cell_dead.npy", N_cells_dead)
        np.save(f"./{log_dir}/results/x_.npy", x_)

    print('plot results ...')

    last_frame_growth = np.argwhere(np.diff(N_cells_alive[:, 0], axis=0))
    last_frame_growth = last_frame_growth[-1] - 1
    N_cells_alive = N_cells_alive[0:int(last_frame_growth), :]
    N_cells_dead = N_cells_dead[0:int(last_frame_growth), :]

    fig, ax = fig_init()
    for k in range(n_particle_types):
        plt.plot(np.arange(last_frame_growth), N_cells_alive[:, k], color=cmap.color(k), linewidth=4,
                 label=f'Cell type {k} alive')
    plt.xlabel(r'Frame', fontsize=64)
    plt.ylabel(r'Number of alive cells', fontsize=64)
    ax.xaxis.set_major_formatter(FormatStrFormatter('%.0f'))
    ax.yaxis.set_major_formatter(FormatStrFormatter('%.0f'))
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/cell_alive_{config_file}.tif", dpi=300)
    plt.close()

    fig, ax = fig_init()
    for k in range(n_particle_types):
        plt.plot(np.arange(last_frame_growth), N_cells_dead[:, k], color=cmap.color(k), linewidth=4,
                 label=f'Cell type {k} dead')
    plt.xlabel(r'Frame', fontsize=64)
    plt.ylabel(r'Number of dead cells', fontsize=64)
    ax.xaxis.set_major_formatter(FormatStrFormatter('%.0f'))
    ax.yaxis.set_major_formatter(FormatStrFormatter('%.0f'))
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/cell_dead_{config_file}.tif", dpi=300)
    plt.close()


    pos = np.argwhere(x_[:, 7:8] > 0)
    pos = pos[:, 0]
    division_list = np.concatenate((x_[pos, 5:6], x_[pos, 7:8]), axis=1)
    reconstructed_cell_cycle_length = np.zeros((n_particle_types, 1))

    for k in range(n_particle_types):
        pos = np.argwhere(division_list[:, 0] == k)
        pos = pos[:, 0]
        if len(pos>0):
            print(f'Cell type {k} division rate: {np.mean(division_list[pos, 1:2])}+/-{np.std(division_list[pos, 1:2])}')
            logger.info(f'Cell type {k} division rate: {np.mean(division_list[pos, 1:2])}+/-{np.std(division_list[pos, 1:2])}')
            reconstructed_cell_cycle_length[k] = np.mean(division_list[pos, 1:2])

    x_data = cell_cycle_length
    y_data = reconstructed_cell_cycle_length.squeeze()
    lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)
    residuals = y_data - linear_model(x_data, *lin_fit)
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
    r_squared = 1 - (ss_res / ss_tot)
    print(f'R^2$: {np.round(r_squared, 3)}  slope: {np.round(lin_fit[0], 2)}')
    logger.info(f'R^2$: {np.round(r_squared, 3)}  slope: {np.round(lin_fit[0], 2)}')

    fig, ax = fig_init(formatx='%.0f', formaty='%.0f')
    plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
    plt.scatter(cell_cycle_length,reconstructed_cell_cycle_length, color=cmap.color(np.arange(n_particle_types)), s=200)
    plt.xlabel(r'True cell cycle length', fontsize=32)
    plt.ylabel(r'Reconstructed cell cycle length', fontsize=32)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/cell_cycle_length_{config_file}.tif", dpi=170)
    plt.close()


def data_plot_attraction_repulsion(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    dimension = config.simulation.dimension
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    max_radius = config.simulation.max_radius
    cmap = CustomColorMap(config=config)
    n_runs = config.training.n_runs

    embedding_cluster = EmbeddingCluster(config)

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    x = x_list[1][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    model, bc_pos, bc_dpos = choose_training_model(config, device)

    for epoch in epoch_list:

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        print(f'network: {net}')
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        model_a_first = model.a.clone().detach()
        config.training.cluster_distance_threshold = 0.01
        accuracy, n_clusters, new_labels = plot_embedding_func_cluster(model, config, config_file, embedding_cluster,
                                                                       cmap, index_particles, type_list,
                                                                       n_particle_types, n_particles, ynorm, epoch,
                                                                       log_dir, device)
        print(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')

        fig, ax = fig_init()
        p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
        rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
        rmserr_list = []
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            embedding_ = model_a_first[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, embedding_), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            true_func = model.psi(rr, p[to_numpy(type_list[n]).astype(int)].squeeze(),
                                  p[to_numpy(type_list[n]).astype(int)].squeeze())
            rmserr_list.append(torch.sqrt(torch.mean((func * ynorm - true_func.squeeze()) ** 2)))
            plt.plot(to_numpy(rr),
                     to_numpy(func) * to_numpy(ynorm),
                     color=cmap.color(to_numpy(type_list[n]).astype(int)), linewidth=8, alpha=0.1)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xlim([0, max_radius])
        plt.ylim(config.plotting.ylim)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_all_{config_file}_{epoch}.tif", dpi=170.7)
        rmserr_list = torch.stack(rmserr_list)
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        plt.close()

        fig, ax = fig_init()
        plots = []
        plots.append(rr)
        for n in range(n_particle_types):
            plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n], p[n])), color=cmap.color(n), linewidth=8)
            plots.append(model.psi(rr, p[n], p[n]).squeeze())
        plt.xlim([0, max_radius])
        plt.ylim(config.plotting.ylim)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}.tif", dpi=170.7)
        plt.close()

        rr = torch.tensor(np.linspace(-1.5 * max_radius, 1.5 * max_radius, 2000)).to(device)
        fig, ax = fig_init()
        plots = []
        plots.append(rr)
        for n in range(n_particle_types):
            t = model.psi(rr, p[n], p[n])
            plt.plot(to_numpy(rr), to_numpy(t), color=cmap.color(n), linewidth=8)
            plots.append(model.psi(rr, p[n], p[n]).squeeze())
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xlim([-max_radius * 1.5, max_radius * 1.5])
        plt.ylim(config.plotting.ylim)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.tight_layout()
        torch.save(plots, f"./{log_dir}/results/plots_true_{config_file}_{epoch}.pt")
        plt.close()


def data_plot_attraction_repulsion_tracking(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    dimension = config.simulation.dimension
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    min_radius = config.simulation.min_radius
    max_radius = config.simulation.max_radius
    cmap = CustomColorMap(config=config)
    n_runs = config.training.n_runs
    n_frames = config.simulation.n_frames
    delta_t = config.simulation.delta_t

    embedding_cluster = EmbeddingCluster(config)

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    x = x_list[1][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)
    type_list_first = type_list.clone().detach()

    index_l = []
    index = 0
    for k in range(n_frames):
        new_index = torch.arange(index, index + n_particles)
        index_l.append(new_index)
        x_list[1][k][:, 0] = new_index
        index += n_particles

    model, bc_pos, bc_dpos = choose_training_model(config, device)

    for epoch in epoch_list:
        pos = epoch.find('_')
        if pos>0:
            epoch_ = epoch[0:pos]
        else:
            epoch_ = epoch
        embedding_type = int(epoch_)%2
        print(f'{epoch}, {epoch_}, {embedding_type}')
        logger.info(f'{epoch}, {epoch_}, {embedding_type}')

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        print(f'network: {net}')
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        fig = plt.figure(figsize=(8, 8))
        tracking_index = 0
        tracking_index_list = []
        for k in trange(n_frames):
            x = x_list[1][k].clone().detach()
            distance = torch.sum(bc_dpos(x[:, None, 1:3] - x[None, :, 1:3]) ** 2, dim=2)
            adj_t = ((distance < max_radius ** 2) & (distance > min_radius ** 2)).float() * 1
            edges = adj_t.nonzero().t().contiguous()
            dataset = data.Data(x=x[:, :], edge_index=edges)

            pred = model(dataset, training=True, vnorm=vnorm, phi=torch.zeros(1, device=device))

            x_next = x_list[1][k + 1]
            x_next = x_next[:, 1:3].clone().detach()
            x_pred = (x[:, 1:3] + delta_t * pred)

            distance = torch.sum(bc_dpos(x_pred[:, None, :] - x_next[None, :, :]) ** 2, dim=2)
            result = distance.min(dim=1)
            min_value = result.values
            min_index = result.indices

            true_index = np.arange(len(min_index))
            reconstructed_index = to_numpy(min_index)
            for n in range(n_particle_types):
                plt.scatter(true_index[index_particles[n]], reconstructed_index[index_particles[n]], s=1, color=cmap.color(n), alpha=0.05)

            tracking_index += np.sum((to_numpy(min_index) - np.arange(len(min_index)) == 0)) / n_frames / n_particles * 100
            tracking_index_list.append(np.sum((to_numpy(min_index) - np.arange(len(min_index)) == 0)))
            x_list[1][k + 1][min_index, 0:1] = x_list[1][k][:, 0:1].clone().detach()

        x_ = torch.stack(x_list[1])
        x_ = torch.reshape(x_, (x_.shape[0] * x_.shape[1], x_.shape[2]))
        x_ = x_[0:(n_frames - 1) * n_particles]
        indexes = np.unique(to_numpy(x_[:, 0]))

        plt.xlabel(r'True particle index', fontsize=32)
        plt.ylabel(r'Particle index in next frame', fontsize=32)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/proxy_tracking_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()
        print(f'tracking index: {np.round(tracking_index,3)}')
        logger.info(f'tracking index: {np.round(tracking_index,3)}')
        print(f'{len(indexes)} tracks')
        logger.info(f'{len(indexes)} tracks')

        tracking_index_list = np.array(tracking_index_list)
        tracking_index_list = n_particles - tracking_index_list

        fig,ax = fig_init(formatx='%.0f', formaty='%.0f')
        plt.plot(np.arange(n_frames), tracking_index_list, color='k', linewidth=2)
        plt.ylabel(r'tracking errors', fontsize=64)
        plt.xlabel(r'frame', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/tracking_error_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()

        print(f'tracking errors: {np.sum(tracking_index_list)}')
        logger.info(f'tracking errors: {np.sum(tracking_index_list)}')

        if embedding_type==1:
            type_list = to_numpy(x_[indexes,5])
        else:
            type_list = to_numpy(type_list_first)

        config.training.cluster_distance_threshold = 0.01
        model_a_first = model.a.clone().detach()
        accuracy, n_clusters, new_labels = plot_embedding_func_cluster_tracking(model, config, config_file, embedding_cluster, cmap, index_particles, indexes, type_list,
                                n_particle_types, n_particles, ynorm, epoch, log_dir, embedding_type, device)
        print(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')


        if embedding_type==1:
            fig, ax = fig_init()
            p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
            rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
            rmserr_list = []
            for n in indexes:
                embedding_ = model_a_first[int(n), :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
                in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                         rr[:, None] / max_radius, embedding_), dim=1)
                with torch.no_grad():
                    func = model.lin_edge(in_features.float())
                func = func[:, 0]
                true_func = model.psi(rr, p[int(type_list[n])].squeeze(),
                                      p[int(type_list[n])].squeeze())
                rmserr_list.append(torch.sqrt(torch.mean((func - true_func.squeeze()) ** 2)))
                plt.plot(to_numpy(rr),
                         to_numpy(func),
                         color=cmap.color(int(type_list[int(n)])), linewidth=8, alpha=0.1)
            plt.xlabel(r'$d_{ij}$', fontsize=64)
            plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
            plt.xlim([0, max_radius])
            plt.ylim(config.plotting.ylim)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/func_all_{config_file}_{epoch}.tif", dpi=170.7)
            rmserr_list = torch.stack(rmserr_list)
            rmserr_list = to_numpy(rmserr_list)
            print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
            logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
            plt.close()
        else:
            fig, ax = fig_init()
            p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
            rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
            rmserr_list = []
            for n in range(int(n_particles * (1 - config.training.particle_dropout))):
                embedding_ = model_a_first[n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
                in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                         rr[:, None] / max_radius, embedding_), dim=1)
                with torch.no_grad():
                    func = model.lin_edge(in_features.float())
                func = func[:, 0]
                true_func = model.psi(rr, p[int(type_list[n])].squeeze(),
                                      p[int(type_list[n])].squeeze())
                rmserr_list.append(torch.sqrt(torch.mean((func - true_func.squeeze()) ** 2)))
                plt.plot(to_numpy(rr),
                         to_numpy(func),
                         color=cmap.color(int(type_list[n])), linewidth=8, alpha=0.1)
            plt.xlabel(r'$d_{ij}$', fontsize=64)
            plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
            plt.xlim([0, max_radius])
            plt.ylim(config.plotting.ylim)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/func_all_{config_file}_{epoch}.tif", dpi=170.7)
            rmserr_list = torch.stack(rmserr_list)
            rmserr_list = to_numpy(rmserr_list)
            print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
            logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
            plt.close()

        fig, ax = fig_init()
        for n in range(n_particle_types):
            plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n], p[n])), color=cmap.color(n), linewidth=8)
        plt.xlim([0, max_radius])
        plt.ylim(config.plotting.ylim)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}.tif", dpi=170.7)
        plt.close()


def data_plot_attraction_repulsion_asym(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    dimension = config.simulation.dimension
    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    cmap = CustomColorMap(config=config)
    embedding_cluster = EmbeddingCluster(config)
    n_runs = config.training.n_runs

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    model, bc_pos, bc_dpos = choose_training_model(config, device)

    x = x_list[1][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    for epoch in epoch_list:

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        print(f'network: {net}')
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        model_a_first = model.a.clone().detach()
        accuracy, n_clusters, new_labels = plot_embedding_func_cluster(model, config, config_file, embedding_cluster,
                                                                       cmap, index_particles, type_list,
                                                                       n_particle_types, n_particles, ynorm, epoch,
                                                                       log_dir, device)
        print(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')

        x = x_list[0][100].clone().detach()
        index_particles = get_index_particles(x, n_particle_types, dimension)
        type_list = to_numpy(get_type_list(x, dimension))
        distance = torch.sum(bc_dpos(x[:, None, 1:dimension + 1] - x[None, :, 1:dimension + 1]) ** 2, dim=2)
        adj_t = ((distance < max_radius ** 2) & (distance > min_radius ** 2)).float() * 1
        edges = adj_t.nonzero().t().contiguous()
        indexes = np.random.randint(0, edges.shape[1], 5000)
        edges = edges[:, indexes]

        fig, ax = fig_init()
        rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
        func_list = []
        for n in trange(edges.shape[1]):
            embedding_1 = model.a[1, edges[0, n], :] * torch.ones((1000, config.graph_model.embedding_dim),
                                                                  device=device)
            embedding_2 = model.a[1, edges[1, n], :] * torch.ones((1000, config.graph_model.embedding_dim),
                                                                  device=device)
            type = type_list[to_numpy(edges[0, n])].astype(int)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, embedding_1, embedding_2), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            func_list.append(func)
            plt.plot(to_numpy(rr),
                     to_numpy(func) * to_numpy(ynorm),
                     color=cmap.color(type), linewidth=8)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, \ensuremath{\mathbf{a}}_j, d_{ij})$', fontsize=64)
        plt.ylim(config.plotting.ylim)
        plt.xlim([0, max_radius])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()

        fig, ax = fig_init()
        p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
        true_func = []
        for n in range(n_particle_types):
            for m in range(n_particle_types):
                true_func.append(model.psi(rr, p[n, m].squeeze(), p[n, m].squeeze()))
                plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n,m], p[n,m]).squeeze()), color=cmap.color(n), linewidth=8)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, \ensuremath{\mathbf{a}}_j, d_{ij})$', fontsize=64)
        plt.ylim(config.plotting.ylim)
        plt.xlim([0, max_radius])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}.tif", dpi=170.7)
        plt.close()

        true_func_list = []
        for k in trange(edges.shape[1]):
            n = type_list[to_numpy(edges[0, k])].astype(int)
            m = type_list[to_numpy(edges[1, k])].astype(int)
            true_func_list.append(true_func[3 * n.squeeze() + m.squeeze()])
        func_list = torch.stack(func_list) * ynorm
        true_func_list = torch.stack(true_func_list)
        rmserr_list = torch.sqrt(torch.mean((func_list - true_func_list) ** 2, axis=1))
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))


def data_plot_attraction_repulsion_continuous(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    dimension = config.simulation.dimension
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    dataset_name = config.dataset
    max_radius = config.simulation.max_radius
    n_runs = config.training.n_runs
    cmap = CustomColorMap(config=config)

    embedding_cluster = EmbeddingCluster(config)

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    model, bc_pos, bc_dpos = choose_training_model(config, device)

    x = x_list[1][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    for epoch in epoch_list:

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        print(f'network: {net}')
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])

        n_particle_types = 3
        index_particles = []
        for n in range(n_particle_types):
            index_particles.append(
                np.arange((n_particles // n_particle_types) * n, (n_particles // n_particle_types) * (n + 1)))

        fig, ax = fig_init()
        embedding = get_embedding(model.a, 1)
        csv_ = embedding
        for n in range(n_particle_types):
            plt.scatter(embedding[index_particles[n], 0],
                        embedding[index_particles[n], 1], color=cmap.color(n), s=400, alpha=0.1)
        plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{epoch}.tif", dpi=170.7)
        np.save(f"./{log_dir}/results/embedding_{config_file}_{epoch}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/embedding_{config_file}_{epoch}.txt", csv_)
        plt.close()

        fig, ax = fig_init()
        rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
        func_list = []
        csv_ = []
        csv_.append(to_numpy(rr))
        for n in range(n_particles):
            embedding = model.a[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, embedding), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            func_list.append(func)
            csv = to_numpy(func)
            plt.plot(to_numpy(rr),
                     to_numpy(func) * to_numpy(ynorm),
                     color=cmap.color(n // 1600), linewidth=8, alpha=0.1)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xlim([0, max_radius])
        plt.ylim(config.plotting.ylim)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_{config_file}_{epoch}.tif", dpi=170.7)
        np.save(f"./{log_dir}/results/func_{config_file}_{epoch}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/func_{config_file}_{epoch}.txt", csv_)
        plt.close()

        fig, ax = fig_init()
        p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt')
        true_func_list = []
        csv_ = []
        csv_.append(to_numpy(rr))
        for n in range(n_particles):
            plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n], p[n])), color=cmap.color(n // 1600), linewidth=8,
                     alpha=0.1)
            true_func_list.append(model.psi(rr, p[n], p[n]))
            csv_.append(to_numpy(model.psi(rr, p[n], p[n]).squeeze()))
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xticks(fontsize=32)
        plt.yticks(fontsize=32)
        plt.xlim([0, max_radius])
        plt.ylim(config.plotting.ylim)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}.tif", dpi=170.7)
        np.save(f"./{log_dir}/results/true_func_{config_file}_{epoch}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/true_func_{config_file}_{epoch}.txt", csv_)
        plt.close()

        func_list = torch.stack(func_list) * ynorm
        true_func_list = torch.stack(true_func_list)

        rmserr_list = torch.sqrt(torch.mean((func_list - true_func_list) ** 2, axis=1))
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))


def data_plot_gravity(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    n_runs = config.training.n_runs
    cmap = CustomColorMap(config=config)
    dimension = config.simulation.dimension

    embedding_cluster = EmbeddingCluster(config)

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    x = x_list[0][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    model, bc_pos, bc_dpos = choose_training_model(config, device)

    for epoch in epoch_list:

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        print(f'network: {net}')
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        model_a_first = model.a.clone().detach()
        config.training.cluster_distance_threshold = 0.01
        accuracy, n_clusters, new_labels = plot_embedding_func_cluster(model, config, config_file, embedding_cluster,
                                                                       cmap, index_particles, type_list,
                                                                       n_particle_types, n_particles, ynorm, epoch,
                                                                       log_dir, device)
        print(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')

        fig, ax = fig_init(formatx='%.3f', formaty='%.0f')
        p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
        rr = torch.tensor(np.linspace(min_radius, max_radius, 1000)).to(device)
        rmserr_list = []
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            embedding_ = model_a_first[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                     0 * rr[:, None], 0 * rr[:, None], embedding_), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            true_func = model.psi(rr, p[to_numpy(type_list[n]).astype(int)].squeeze(),
                                  p[to_numpy(type_list[n]).astype(int)].squeeze())
            rmserr_list.append(torch.sqrt(torch.mean((func * ynorm - true_func.squeeze()) ** 2)))
            plt.plot(to_numpy(rr),
                     to_numpy(func) * to_numpy(ynorm),
                     color=cmap.color(to_numpy(type_list[n]).astype(int)), linewidth=8, alpha=0.1)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xlim([0, 0.02])
        plt.ylim([0, 0.5E6])
        ax.xaxis.set_major_formatter(FormatStrFormatter('%.3f'))
        ax.yaxis.set_major_formatter(FormatStrFormatter('%.0f'))
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_all_{config_file}_{epoch}.tif", dpi=170.7)
        rmserr_list = torch.stack(rmserr_list)
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        plt.close()

        fig, ax = fig_init(formatx='%.3f', formaty='%.0f')
        plots = []
        plots.append(rr)
        for n in range(n_particle_types):
            plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n], p[n])), color=cmap.color(n), linewidth=8)
            plots.append(model.psi(rr, p[n], p[n]).squeeze())
        plt.xlim([0, 0.02])
        plt.ylim([0, 0.5E6])
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}.tif", dpi=170.7)
        plt.close()

        rr = torch.tensor(np.linspace(min_radius, max_radius, 1000)).to(device)
        plot_list = []
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            embedding_ = model_a_first[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                     0 * rr[:, None], 0 * rr[:, None], embedding_), dim=1)
            with torch.no_grad():
                pred = model.lin_edge(in_features.float())
            pred = pred[:, 0]
            plot_list.append(pred * ynorm)
        p = np.linspace(0.5, 5, n_particle_types)
        p_list = p[to_numpy(type_list).astype(int)]
        popt_list = []
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            popt, pcov = curve_fit(power_model, to_numpy(rr), to_numpy(plot_list[n]))
            popt_list.append(popt)
        popt_list=np.array(popt_list)

        x_data = p_list.squeeze()
        y_data = popt_list[:, 0]
        lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)

        threshold = 0.4
        relative_error = np.abs(y_data - x_data) / x_data
        pos = np.argwhere(relative_error < threshold)
        pos_outliers = np.argwhere(relative_error > threshold)
        x_data_ = x_data[pos[:, 0]]
        y_data_ = y_data[pos[:, 0]]
        lin_fit, lin_fitv = curve_fit(linear_model, x_data_, y_data_)
        residuals = y_data_ - linear_model(x_data_, *lin_fit)
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data_)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)
        print(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')
        logger.info(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')

        fig, ax = fig_init()
        csv_ = []
        csv_.append(p_list)
        csv_.append(popt_list[:, 0])
        plt.plot(p_list, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(p_list, popt_list[:, 0], color='k', s=50, alpha=0.5)
        plt.scatter(p_list[pos_outliers[:, 0]], popt_list[pos_outliers[:, 0], 0], color='r', s=50)
        plt.xlabel(r'True mass ', fontsize=64)
        plt.ylabel(r'Reconstructed mass ', fontsize=64)
        plt.xlim([0, 5.5])
        plt.ylim([0, 5.5])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/mass_{config_file}.tif", dpi=170)
        # csv_ = np.array(csv_)
        # np.save(f"./{log_dir}/results/mass_{config_file}.npy", csv_)
        # np.savetxt(f"./{log_dir}/results/mass_{config_file}.txt", csv_)
        plt.close()

        relative_error = np.abs(popt_list[:, 0] - p_list.squeeze()) / p_list.squeeze() * 100

        print(f'mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        print(f'mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')
        logger.info(f'mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        logger.info(f'mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')


        fig, ax = fig_init()
        csv_ = []
        csv_.append(p_list.squeeze())
        csv_.append(-popt_list[:, 1])
        csv_ = np.array(csv_)
        plt.plot(p_list, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(p_list, -popt_list[:, 1], color='k', s=50, alpha=0.5)
        plt.xlim([0, 5.5])
        plt.ylim([-4, 0])
        plt.xlabel(r'True mass', fontsize=64)
        plt.ylabel(r'Reconstructed exponent', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/exponent_{config_file}.tif", dpi=170)
        np.save(f"./{log_dir}/results/exponent_{config_file}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/exponent_{config_file}.txt", csv_)
        plt.close()

        print(f'exponent: {np.round(np.mean(-popt_list[:, 1]), 2)}+/-{np.round(np.std(-popt_list[:, 1]), 2)}')
        logger.info(f'mass relative error: {np.round(np.mean(-popt_list[:, 1]), 2)}+/-{np.round(np.std(-popt_list[:, 1]), 2)}')

        text_trap = StringIO()
        sys.stdout = text_trap
        popt_list = []
        for n in range(0,int(n_particles * (1 - config.training.particle_dropout))):
            model_pysrr, max_index, max_value = symbolic_regression(rr, plot_list[n])
            # print(f'{p_list[n].squeeze()}/x0**2, {model_pysrr.sympy(max_index)}')
            logger.info(f'{np.round(p_list[n].squeeze(),2)}/x0**2, pysrr found {model_pysrr.sympy(max_index)}')

            expr = model_pysrr.sympy(max_index).as_terms()[0]
            popt_list.append(expr[0][1][0][0])

        np.save(f"./{log_dir}/results/coeff_pysrr.npy", popt_list)

        sys.stdout = sys.__stdout__

        popt_list = np.array(popt_list)

        x_data = p_list.squeeze()
        y_data = popt_list
        lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)

        threshold = 0.4
        relative_error = np.abs(y_data - x_data) / x_data
        pos = np.argwhere(relative_error < threshold)
        pos_outliers = np.argwhere(relative_error > threshold)
        x_data_ = x_data[pos[:, 0]]
        y_data_ = y_data[pos[:, 0]]
        lin_fit, lin_fitv = curve_fit(linear_model, x_data_, y_data_)
        residuals = y_data_ - linear_model(x_data_, *lin_fit)
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data_)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)
        print(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')
        logger.info(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')

        fig, ax = fig_init()
        csv_ = []
        csv_.append(p_list)
        csv_.append(popt_list)
        plt.plot(p_list, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(p_list, popt_list, color='k', s=50, alpha=0.5)
        plt.xlabel(r'True mass ', fontsize=64)
        plt.ylabel(r'Reconstructed mass ', fontsize=64)
        plt.xlim([0, 5.5])
        plt.ylim([0, 5.5])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/pysrr_mass_{config_file}.tif", dpi=300)
        # csv_ = np.array(csv_)
        # np.save(f"./{log_dir}/results/mass_{config_file}.npy", csv_)
        # np.savetxt(f"./{log_dir}/results/mass_{config_file}.txt", csv_)
        plt.close()

        relative_error = np.abs(popt_list - p_list.squeeze()) / p_list.squeeze() * 100

        print(f'pysrr_mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        print(f'pysrr_mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')
        logger.info(f'pysrr_mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        logger.info(f'pysrr_mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')


def data_plot_gravity_continuous(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    n_runs = config.training.n_runs
    dimension= config.simulation.dimension
    cmap = CustomColorMap(config=config)

    embedding_cluster = EmbeddingCluster(config)

    time.sleep(0.5)

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    model, bc_pos, bc_dpos = choose_training_model(config, device)

    x = x_list[1][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    for epoch in epoch_list:

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        embedding = get_embedding(model.a, 1)
        fig, ax = fig_init()
        for n in range(n_particle_types):
            plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], color=cmap.color(n % 256),
                        s=400,
                        alpha=0.1)
        plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}.tif", dpi=170)
        plt.close()

        fig, ax = fig_init(formatx='%.3f', formaty='%.0f')
        p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
        rr = torch.tensor(np.linspace(min_radius, max_radius, 1000)).to(device)
        rmserr_list = []
        csv_ = []
        csv_.append(to_numpy(rr))
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            embedding_ = model.a[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                     0 * rr[:, None], 0 * rr[:, None], embedding_), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            csv_.append(to_numpy(func))
            true_func = model.psi(rr, p[to_numpy(type_list[n]).astype(int)].squeeze(),
                                  p[to_numpy(type_list[n]).astype(int)].squeeze())
            rmserr_list.append(torch.sqrt(torch.mean((func * ynorm - true_func.squeeze()) ** 2)))
            plt.plot(to_numpy(rr),
                     to_numpy(func) * to_numpy(ynorm),
                     color=cmap.color(n % 256), linewidth=8, alpha=0.1)
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xlim([0, max_radius])
        plt.xlim([0, 0.02])
        plt.ylim([0, 0.5E6])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_{config_file}.tif", dpi=170)
        csv_ = np.array(csv_)
        np.save(f"./{log_dir}/results/func_{config_file}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/func_{config_file}.txt", csv_)
        plt.close()

        rmserr_list = torch.stack(rmserr_list)
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))

        fig, ax = fig_init(formatx='%.3f', formaty='%.0f')
        p = np.linspace(0.5, 5, n_particle_types)
        p = torch.tensor(p, device=device)
        csv_ = []
        csv_.append(to_numpy(rr))
        for n in range(n_particle_types - 1, -1, -1):
            plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n], p[n])), color=cmap.color(n % 256), linewidth=8)
            csv_.append(to_numpy(model.psi(rr, p[n], p[n]).squeeze()))
        plt.xlim([0, 0.02])
        plt.ylim([0, 0.5E6])
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}.tif", dpi=300)
        csv_ = np.array(csv_)
        np.save(f"./{log_dir}/results/true_func_{config_file}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/true_func_{config_file}.txt", csv_)
        plt.close()

        rr = torch.tensor(np.linspace(min_radius, max_radius, 1000)).to(device)
        plot_list = []
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            embedding_ = model.a[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                     0 * rr[:, None], 0 * rr[:, None], embedding_), dim=1)
            with torch.no_grad():
                pred = model.lin_edge(in_features.float())
            pred = pred[:, 0]
            plot_list.append(pred * ynorm)
        p = np.linspace(0.5, 5, n_particle_types)
        p_list = p[to_numpy(type_list).astype(int)]
        popt_list = []
        for n in range(int(n_particles * (1 - config.training.particle_dropout))):
            popt, pcov = curve_fit(power_model, to_numpy(rr), to_numpy(plot_list[n]))
            popt_list.append(popt)
        popt_list=np.array(popt_list)

        x_data = p_list.squeeze()
        y_data = popt_list[:, 0]
        lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)

        threshold = 0.4
        relative_error = np.abs(y_data - x_data) / x_data
        pos = np.argwhere(relative_error < threshold)
        pos_outliers = np.argwhere(relative_error > threshold)
        x_data_ = x_data[pos[:, 0]]
        y_data_ = y_data[pos[:, 0]]
        lin_fit, lin_fitv = curve_fit(linear_model, x_data_, y_data_)
        residuals = y_data_ - linear_model(x_data_, *lin_fit)
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data_)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)
        print(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')
        logger.info(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')

        fig, ax = fig_init()
        plt.plot(p_list, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(p_list, popt_list[:, 0], color='k', s=50, alpha=0.5)
        plt.scatter(p_list[pos_outliers[:, 0]], popt_list[pos_outliers[:, 0], 0], color='r', s=50)
        plt.xlabel(r'True mass ', fontsize=64)
        plt.ylabel(r'Reconstructed mass ', fontsize=64)
        plt.xlim([0, 5.5])
        plt.ylim([0, 5.5])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/mass_{config_file}.tif", dpi=300)
        plt.close()

        relative_error = np.abs(popt_list[:, 0] - p_list.squeeze()) / p_list.squeeze() * 100

        print(f'mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        print(f'mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')
        logger.info(f'mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        logger.info(f'mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')


        fig, ax = fig_init()
        csv_ = []
        csv_.append(p_list.squeeze())
        csv_.append(-popt_list[:, 1])
        csv_ = np.array(csv_)
        plt.plot(p_list, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(p_list, -popt_list[:, 1], color='k', s=50, alpha=0.5)
        plt.xlim([0, 5.5])
        plt.ylim([-4, 0])
        plt.xlabel(r'True mass', fontsize=64)
        plt.ylabel(r'Reconstructed exponent', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/exponent_{config_file}.tif", dpi=300)
        np.save(f"./{log_dir}/results/exponent_{config_file}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/exponent_{config_file}.txt", csv_)
        plt.close()

        print(f'exponent: {np.round(np.mean(-popt_list[:, 1]), 2)}+/-{np.round(np.std(-popt_list[:, 1]), 2)}')
        logger.info(f'mass relative error: {np.round(np.mean(-popt_list[:, 1]), 2)}+/-{np.round(np.std(-popt_list[:, 1]), 2)}')

        text_trap = StringIO()
        sys.stdout = text_trap
        popt_list = []
        for n in range(0,int(n_particles * (1 - config.training.particle_dropout))):
            print(n)
            model_pysrr, max_index, max_value = symbolic_regression(rr, plot_list[n])
            # print(f'{p_list[n].squeeze()}/x0**2, {model_pysrr.sympy(max_index)}')
            logger.info(f'{np.round(p_list[n].squeeze(),2)}/x0**2, pysrr found {model_pysrr.sympy(max_index)}')

            expr = model_pysrr.sympy(max_index).as_terms()[0]
            popt_list.append(expr[0][1][0][0])

        np.save(f"./{log_dir}/results/coeff_pysrr.npy", popt_list)

        sys.stdout = sys.__stdout__

        popt_list = np.array(popt_list)

        x_data = p_list.squeeze()
        y_data = popt_list
        lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)

        threshold = 0.4
        relative_error = np.abs(y_data - x_data) / x_data
        pos = np.argwhere(relative_error < threshold)
        pos_outliers = np.argwhere(relative_error > threshold)
        x_data_ = x_data[pos[:, 0]]
        y_data_ = y_data[pos[:, 0]]
        lin_fit, lin_fitv = curve_fit(linear_model, x_data_, y_data_)
        residuals = y_data_ - linear_model(x_data_, *lin_fit)
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data_)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)
        print(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')
        logger.info(f'R^2$: {np.round(r_squared, 2)}  Slope: {np.round(lin_fit[0], 2)}  outliers: {np.sum(relative_error > threshold)}  ')

        fig, ax = fig_init()
        csv_ = []
        csv_.append(p_list)
        csv_.append(popt_list)
        plt.plot(p_list, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(p_list, popt_list, color='k', s=50, alpha=0.5)
        plt.xlabel(r'True mass ', fontsize=64)
        plt.ylabel(r'Reconstructed mass ', fontsize=64)
        plt.xlim([0, 5.5])
        plt.ylim([0, 5.5])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/pysrr_mass_{config_file}.tif", dpi=300)
        # csv_ = np.array(csv_)
        # np.save(f"./{log_dir}/results/mass_{config_file}.npy", csv_)
        # np.savetxt(f"./{log_dir}/results/mass_{config_file}.txt", csv_)
        plt.close()

        relative_error = np.abs(popt_list - p_list.squeeze()) / p_list.squeeze() * 100

        print(f'pysrr_mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        print(f'pysrr_mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')
        logger.info(f'pysrr_mass relative error: {np.round(np.mean(relative_error), 2)}+/-{np.round(np.std(relative_error), 2)}')
        logger.info(f'pysrr_mass relative error wo outliers: {np.round(np.mean(relative_error[pos[:, 0]]), 2)}+/-{np.round(np.std(relative_error[pos[:, 0]]), 2)}')


def data_plot_gravity_solar_system(config_file, epoch_list, log_dir, logger, device):
    config_file = 'gravity_solar_system'
    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')

    dataset_name = config.dataset
    embedding_cluster = EmbeddingCluster(config)

    cmap = CustomColorMap(config=config)
    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    n_runs = config.training.n_runs
    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius

    time.sleep(0.5)

    x_list = []
    y_list = []
    print('Load data ...')
    time.sleep(1)
    x_list.append(torch.load(f'graphs_data/graphs_{dataset_name}/x_list_0.pt', map_location=device))
    y_list.append(torch.load(f'graphs_data/graphs_{dataset_name}/y_list_0.pt', map_location=device))
    vnorm = torch.load(os.path.join(log_dir, 'vnorm.pt'), map_location=device)
    ynorm = torch.load(os.path.join(log_dir, 'ynorm.pt'), map_location=device)
    x = x_list[0][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    model, bc_pos, bc_dpos = choose_training_model(config, device)

    net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_2.pt"
    state_dict = torch.load(net, map_location=device)
    model.load_state_dict(state_dict['model_state_dict'])
    model.eval()

    plt.rcParams['text.usetex'] = True
    rc('font', **{'family': 'serif', 'serif': ['Palatino']})
    # matplotlib.use("Qt5Agg")

    fig = plt.figure(figsize=(10.5, 9.6))
    plt.ion()
    ax = fig.add_subplot(3, 3, 1)
    embedding = plot_embedding('a)', model.a, 1, index_particles, n_particles, n_particle_types, 20, '$10^6$', fig, ax,
                               cmap, device)

    ax = fig.add_subplot(3, 3, 2)
    rr = torch.tensor(np.linspace(min_radius, max_radius, 1000)).to(device)
    func_list = plot_function(True, 'b)', config.graph_model.particle_model_name, model.lin_edge,
                              model.a, 1, to_numpy(x[:, 5]).astype(int), rr, max_radius, ynorm, index_particles,
                              n_particles, n_particle_types, 20, '$10^6$', fig, ax, cmap, device)

    ax = fig.add_subplot(3, 3, 3)

    it = 2000
    x0 = x_list[0][it].clone().detach()
    y0 = y_list[0][it].clone().detach()
    x = x_list[0][it].clone().detach()
    distance = torch.sum(bc_dpos(x[:, None, 1:3] - x[None, :, 1:3]) ** 2, dim=2)
    t = torch.Tensor([max_radius ** 2])  # threshold
    adj_t = ((distance < max_radius ** 2) & (distance > min_radius ** 2)) * 1.0
    edge_index = adj_t.nonzero().t().contiguous()
    dataset = data.Data(x=x, edge_index=edge_index)

    with torch.no_grad():
        y = model(dataset, data_id=1, training=False, vnorm=vnorm,
                  phi=torch.zeros(1, device=device))  # acceleration estimation
    y = y * ynorm

    proj_interaction, new_labels, n_clusters = plot_umap('b)', func_list, log_dir, 500, index_particles,
                                                         n_particles, n_particle_types, embedding_cluster, 20, '$10^6$',
                                                         fig, ax, cmap, device)

    ax = fig.add_subplot(3, 3, 3)
    accuracy = plot_confusion_matrix('c)', to_numpy(x[:, 5:6]), new_labels, n_particle_types, 20, '$10^6$', fig, ax)
    plt.tight_layout()

    model_a_ = model.a.clone().detach()
    model_a_ = torch.reshape(model_a_, (model_a_.shape[0] * model_a_.shape[1], model_a_.shape[2]))
    for k in range(n_clusters):
        pos = np.argwhere(new_labels == k).squeeze().astype(int)
        temp = model_a_[pos, :].clone().detach()
        model_a_[pos, :] = torch.median(temp, dim=0).values.repeat((len(pos), 1))
    with torch.no_grad():
        for n in range(model.a.shape[0]):
            model.a[n] = model_a_
    embedding, embedding_particle = get_embedding(model.a, 1)

    ax = fig.add_subplot(3, 3, 4)
    plt.text(-0.25, 1.1, f'd)', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    plt.title(r'Clustered particle embedding', fontsize=12)
    for n in range(n_particle_types):
        pos = np.argwhere(new_labels == n).squeeze().astype(int)
        plt.scatter(embedding[pos[0], 0], embedding[pos[0], 1], color=cmap.color(n), s=6)
    plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=12)
    plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=12)
    plt.xticks(fontsize=10.0)
    plt.yticks(fontsize=10.0)
    plt.text(.05, .94, f'e: 20 it: $10^6$', ha='left', va='top', transform=ax.transAxes, fontsize=10)

    ax = fig.add_subplot(3, 3, 5)
    print('5')
    plt.text(-0.25, 1.1, f'e)', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    plt.title(r'Interaction functions (model)', fontsize=12)
    func_list = []
    for n in range(n_particle_types):
        pos = np.argwhere(new_labels == n).squeeze().astype(int)
        embedding = model.a[0, pos[0], :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
        in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                 rr[:, None] / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                 0 * rr[:, None], 0 * rr[:, None], embedding), dim=1)
        with torch.no_grad():
            func = model.lin_edge(in_features.float())
        func = func[:, 0]
        func_list.append(func)
        plt.plot(to_numpy(rr),
                 to_numpy(func) * to_numpy(ynorm),
                 color=cmap.color(n), linewidth=1)
    plt.xlabel(r'$d_{ij}$', fontsize=12)
    plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij}$', fontsize=12)
    plt.xticks(fontsize=10.0)
    plt.yticks(fontsize=10.0)
    plt.xlim([0, 0.02])
    plt.ylim([0, 0.5E6])
    plt.text(.05, .94, f'e: 20 it: $10^6$', ha='left', va='top', transform=ax.transAxes, fontsize=10)

    ax = fig.add_subplot(3, 3, 6)
    print('6')
    plt.text(-0.25, 1.1, f'k)', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    plt.title(r'Interaction functions (true)', fontsize=12)
    p = np.linspace(0.5, 5, n_particle_types)
    p = torch.tensor(p, device=device)
    for n in range(n_particle_types - 1, -1, -1):
        plt.plot(to_numpy(rr), to_numpy(model.psi(rr, p[n], p[n])), color=cmap.color(n), linewidth=1)
    plt.xlabel(r'$d_{ij}$', fontsize=12)
    plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij}$', fontsize=12)
    plt.xticks(fontsize=10.0)
    plt.yticks(fontsize=10.0)
    plt.xlim([0, 0.02])
    plt.ylim([0, 0.5E6])

    plot_list = []
    for n in range(n_particle_types):
        pos = np.argwhere(new_labels == n).squeeze().astype(int)
        embedding = model.a[0, pos[0], :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
        if config.graph_model.prediction == '2nd_derivative':
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                     0 * rr[:, None], 0 * rr[:, None], embedding), dim=1)
        else:
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, embedding), dim=1)
        with torch.no_grad():
            pred = model.lin_edge(in_features.float())
        pred = pred[:, 0]
        plot_list.append(pred * ynorm)
    p = np.linspace(0.5, 5, n_particle_types)
    popt_list = []
    for n in range(n_particle_types):
        popt, pcov = curve_fit(power_model, to_numpy(rr), to_numpy(plot_list[n]))
        popt_list.append(popt)
    popt_list = np.array(popt_list)

    ax = fig.add_subplot(3, 3, 7)
    print('7')
    plt.text(-0.25, 1.1, f'g)', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    x_data = p
    y_data = popt_list[:, 0]
    lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)
    plt.plot(p, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=0.5)
    plt.scatter(p, popt_list[:, 0], color='k', s=20)
    plt.title(r'Reconstructed masses', fontsize=12)
    plt.xlabel(r'True mass ', fontsize=12)
    plt.ylabel(r'Predicted mass ', fontsize=12)
    plt.xlim([0, 5.5])
    plt.ylim([0, 5.5])
    plt.text(0.5, 5, f"Slope: {np.round(lin_fit[0], 2)}", fontsize=10)
    residuals = y_data - linear_model(x_data, *lin_fit)
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
    r_squared = 1 - (ss_res / ss_tot)
    plt.text(0.5, 4.5, f"$R^2$: {np.round(r_squared, 3)}", fontsize=10)

    ax = fig.add_subplot(3, 3, 8)
    print('8')
    plt.text(-0.25, 1.1, f'h)', ha='left', va='top', transform=ax.transAxes, fontsize=12)
    plt.scatter(p, -popt_list[:, 1], color='k', s=20)
    plt.xlim([0, 5.5])
    plt.ylim([-4, 0])
    plt.title(r'Reconstructed exponent', fontsize=12)
    plt.xlabel(r'True mass ', fontsize=12)
    plt.ylabel(r'Exponent fit ', fontsize=12)
    plt.text(0.5, -0.5, f"Exponent: {np.round(np.mean(-popt_list[:, 1]), 3)}+/-{np.round(np.std(popt_list[:, 1]), 3)}",
             fontsize=10)

    # find last image file in logdir
    ax = fig.add_subplot(3, 3, 9)
    files = glob.glob(os.path.join(log_dir, 'tmp_recons/Fig*.tif'))
    files.sort(key=os.path.getmtime)
    if len(files) > 0:
        last_file = files[-1]
        # load image file with imageio
        image = imageio.imread(last_file)
        print('12')
        plt.text(-0.25, 1.1, f'l)', ha='left', va='top', transform=ax.transAxes, fontsize=12)
        plt.title(r'Rollout inference (frame 1000)', fontsize=12)
        plt.imshow(image)
        # rmove xtick
        plt.xticks([])
        plt.yticks([])

    time.sleep(1)
    plt.tight_layout()
    # plt.savefig('Fig3.pdf', format="pdf", dpi=300)
    plt.savefig('Fig3.jpg', dpi=300)
    plt.close()


def data_plot_Coulomb(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    n_runs = config.training.n_runs
    cmap = CustomColorMap(config=config)
    dimension = config.simulation.dimension

    embedding_cluster = EmbeddingCluster(config)

    x_list, y_list, vnorm, ynorm = load_training_data(dataset_name, n_runs, log_dir, device)
    logger.info("vnorm:{:.2e},  ynorm:{:.2e}".format(to_numpy(vnorm), to_numpy(ynorm)))
    x = x_list[0][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)

    model, bc_pos, bc_dpos = choose_training_model(config, device)

    for epoch in epoch_list:

        net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_{epoch}.pt"
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        model_a_first = model.a.clone().detach()
        config.training.cluster_distance_threshold = 0.01
        config.training.cluster_method = 'distance_embedding'
        accuracy, n_clusters, new_labels = plot_embedding_func_cluster(model, config, config_file, embedding_cluster, cmap,
                                                                   index_particles, type_list,
                                                                   n_particle_types, n_particles, ynorm, epoch, log_dir,
                                                                   device)

        print(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')

        x = x_list[0][100].clone().detach()
        index_particles = get_index_particles(x, n_particle_types, dimension)
        type_list = to_numpy(get_type_list(x, dimension))
        distance = torch.sum(bc_dpos(x[:, None, 1:dimension + 1] - x[None, :, 1:dimension + 1]) ** 2, dim=2)
        adj_t = ((distance < max_radius ** 2) & (distance > min_radius ** 2)).float() * 1
        edges = adj_t.nonzero().t().contiguous()
        indexes = np.random.randint(0, edges.shape[1], 5000)
        edges = edges[:, indexes]

        p = [2, 1, -1]

        fig, ax = fig_init(formatx='%.3f', formaty='%.0f')
        func_list = []
        rr = torch.tensor(np.linspace(min_radius, max_radius, 1000)).to(device)
        table_qiqj = np.zeros((10,1))
        tmp = np.array([-2, -1, 1, 2, 4])
        table_qiqj[tmp.astype(int)+2]=np.arange(5)[:,None]
        qiqj_list=[]
        for n in trange(edges.shape[1]):
            embedding_1 = model.a[1, edges[0, n], :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            embedding_2 = model.a[1, edges[1, n], :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            qiqj = p[type_list[to_numpy(edges[0, n])].astype(int).squeeze()] * p[type_list[to_numpy(edges[1, n])].astype(int).squeeze()]
            qiqj_list.append(qiqj)
            type = table_qiqj[qiqj+2].astype(int).squeeze()
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     rr[:, None] / max_radius, embedding_1, embedding_2), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            func_list.append(func * ynorm)
            plt.plot(to_numpy(rr),
                     to_numpy(func) * to_numpy(ynorm),
                     color=cmap.color(type), linewidth=8, alpha=0.1)

        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, \ensuremath{\mathbf{a}}_j, d_{ij})$', fontsize=64)
        plt.xlim([0, 0.02])
        plt.ylim([-0.5E6, 0.5E6])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_{config_file}_{epoch}.tif", dpi=170.7)
        plt.close()

        fig, ax = fig_init(formatx='%.3f', formaty='%.0f')
        csv_ = []
        csv_.append(to_numpy(rr))
        true_func_list = []
        for n in trange(edges.shape[1]):
            temp = model.psi(rr, p[type_list[to_numpy(edges[0, n])].astype(int).squeeze()], p[type_list[to_numpy(edges[1, n])].astype(int).squeeze()] )
            true_func_list.append(temp)
            type = p[type_list[to_numpy(edges[0, n])].astype(int).squeeze()] * p[type_list[to_numpy(edges[1, n])].astype(int).squeeze()]
            type = table_qiqj[type+2].astype(int).squeeze()
            plt.plot(to_numpy(rr), np.array(temp.cpu()), linewidth=8, color=cmap.color(type))
            csv_.append(to_numpy(temp.squeeze()))
        plt.xlim([0, 0.02])
        plt.ylim([-0.5E6, 0.5E6])
        plt.xlabel(r'$d_{ij}$', fontsize=64)
        plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, \ensuremath{\mathbf{a}}_j, d_{ij})$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_{config_file}_{epoch}.tif", dpi=170.7)
        np.save(f"./{log_dir}/results/true_func_{config_file}_{epoch}.npy", csv_)
        np.savetxt(f"./{log_dir}/results/true_func_{config_file}_{epoch}.txt", csv_)
        plt.close()

        func_list = torch.stack(func_list)
        true_func_list = torch.stack(true_func_list)
        rmserr_list = torch.sqrt(torch.mean((func_list - true_func_list) ** 2, axis=1))
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))

        if os.path.exists(f"./{log_dir}/results/coeff_pysrr.npy"):
            popt_list = np.load(f"./{log_dir}/results/coeff_pysrr.npy")
            qiqj_list = np.load(f"./{log_dir}/results/qiqj.npy")
            qiqj=[]
            for n in range(0,len(qiqj_list),5):
                qiqj.append(qiqj_list[n])
            qiqj_list = np.array(qiqj)

        else:
            print('curve fitting ...')
            text_trap = StringIO()
            sys.stdout = text_trap
            popt_list = []
            qiqj_list = np.array(qiqj_list)
            for n in range(0,edges.shape[1],5):
                model_pysrr, max_index, max_value = symbolic_regression(rr, func_list[n])
                print(f'{-qiqj_list[n]}/x0**2, {model_pysrr.sympy(max_index)}')
                logger.info(f'{-qiqj_list[n]}/x0**2, pysrr found {model_pysrr.sympy(max_index)}')

                expr = model_pysrr.sympy(max_index).as_terms()[0]
                popt_list.append(-expr[0][1][0][0])

            np.save(f"./{log_dir}/results/coeff_pysrr.npy", popt_list)
            np.save(f"./{log_dir}/results/qiqj.npy", qiqj_list)

        threshold = 0.25

        fig, ax = fig_init(formatx='%.0f', formaty='%.0f')
        x_data = qiqj_list.squeeze()
        y_data = popt_list.squeeze()

        lin_fit, r_squared, relative_error, outliers, x_data, y_data = linear_fit(x_data, y_data, threshold)

        plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.scatter(qiqj_list, popt_list, color='k', s=200, alpha=0.1)
        plt.xlim([-5, 5])
        plt.ylim([-5, 5])
        plt.xlabel(r'Reconstructed $q_i q_j$', fontsize=32)
        plt.ylabel(r'True $q_i q_j$', fontsize=32)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/qiqj_{config_file}_{epoch}.tif", dpi=1.7)
        plt.close()
        logger.info(
            f'cohesion slope: {np.round(lin_fit[0], 2)}  R^2$: {np.round(r_squared, 3)}  outliers: {np.sum(relative_error > threshold)} ')


def data_plot_boids(config_file, epoch_list, log_dir, logger, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_particle_types = config.simulation.n_particle_types
    n_runs = config.training.n_runs
    has_cell_division = config.simulation.has_cell_division
    cmap = CustomColorMap(config=config)
    n_frames = config.simulation.n_frames
    dimension = config.simulation.dimension

    embedding_cluster = EmbeddingCluster(config)

    print('load data ...')
    x_list = []
    y_list = []
    x_list.append(torch.load(f'graphs_data/graphs_{dataset_name}/x_list_1.pt', map_location=device))
    y_list.append(torch.load(f'graphs_data/graphs_{dataset_name}/y_list_1.pt', map_location=device))
    vnorm = torch.load(os.path.join(log_dir, 'vnorm.pt'), map_location=device)
    ynorm = torch.load(os.path.join(log_dir, 'ynorm.pt'), map_location=device)
    x = x_list[0][-1].clone().detach()

    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)
    n_particles = x.shape[0]
    if has_cell_division:
        n_particles_max = np.load(os.path.join(log_dir, 'n_particles_max.npy'))
        config.simulation.n_particles_max = n_particles_max

    for epoch in epoch_list:

        model, bc_pos, bc_dpos = choose_training_model(config, device)
        model = Interaction_Particles_extract(config, device, aggr_type=config.graph_model.aggr_type, bc_dpos=bc_dpos)

        net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_{epoch}.pt"
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.eval()

        accuracy, n_clusters, new_labels = plot_embedding_func_cluster(model, config, config_file, embedding_cluster,
                                                                       cmap, index_particles, type_list,
                                                                       n_particle_types, n_particles, ynorm, epoch,
                                                                       log_dir, device)
        print(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'final result     accuracy: {np.round(accuracy, 2)}    n_clusters: {n_clusters}    obtained with  method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')

        if has_cell_division:
            plot_cell_rates(config, device, log_dir, n_particle_types, x_list, new_labels, cmap, logger)

        lin_edge_out_list = []
        type_list = []
        diffx_list = []
        diffv_list = []
        cohesion_list=[]
        alignment_list=[]
        separation_list=[]
        r_list = []
        for it in range(0,n_frames//2,n_frames//40):
            print(it)
            x = x_list[0][it].clone().detach()
            particle_index = to_numpy(x[:, 0:1]).astype(int)
            x[:, 5:6] = torch.tensor(new_labels[particle_index],
                                     device=device)  # set label found by clustering and mapperd to ground truth
            pos = torch.argwhere(x[:, 5:6] < n_particle_types).squeeze()
            pos = to_numpy(pos[:, 0]).astype(int)  # filter out cluster not associated with ground truth
            x = x[pos, :]
            distance = torch.sum(bc_dpos(x[:, None, 1:3] - x[None, :, 1:3]) ** 2, dim=2)  # threshold
            adj_t = ((distance < max_radius ** 2) & (distance > min_radius ** 2)) * 1.0
            edge_index = adj_t.nonzero().t().contiguous()
            dataset = data.Data(x=x, edge_index=edge_index)
            with torch.no_grad():
                y, in_features, lin_edge_out = model(dataset, data_id=1, training=False, vnorm=vnorm,
                                                     phi=torch.zeros(1, device=device))  # acceleration estimation
            y = y * ynorm
            lin_edge_out = lin_edge_out * ynorm

            # compute ground truth output
            p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
            model_B = PDE_B_extract(aggr_type=config.graph_model.aggr_type, p=torch.squeeze(p), bc_dpos=bc_dpos)
            rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
            psi_output = []
            for n in range(n_particle_types):
                with torch.no_grad():
                    psi_output.append(model.psi(rr, torch.squeeze(p[n])))
                    y_B, sum, cohesion, alignment, separation, diffx, diffv, r, type = model_B(dataset)  # acceleration estimation

            if it==0:
                lin_edge_out_list=lin_edge_out
                diffx_list=diffx
                diffv_list=diffv
                r_list=r
                type_list=type
                cohesion_list = cohesion
                alignment_list = alignment
                separation_list = separation
            else:
                lin_edge_out_list=torch.cat((lin_edge_out_list,lin_edge_out),dim=0)
                diffx_list=torch.cat((diffx_list,diffx),dim=0)
                diffv_list=torch.cat((diffv_list,diffv),dim=0)
                r_list=torch.cat((r_list,r),dim=0)
                type_list=torch.cat((type_list,type),dim=0)
                cohesion_list=torch.cat((cohesion_list,cohesion),dim=0)
                alignment_list=torch.cat((alignment_list,alignment),dim=0)
                separation_list=torch.cat((separation_list,separation),dim=0)

        type_list = to_numpy(type_list)

        print(f'fitting with known functions {len(type_list)} points ...')
        cohesion_fit = np.zeros(n_particle_types)
        alignment_fit = np.zeros(n_particle_types)
        separation_fit = np.zeros(n_particle_types)
        indexes = np.unique(type_list)
        indexes = indexes.astype(int)

        if False:
            for n in indexes:
                pos = np.argwhere(type_list == n)
                pos = pos[:, 0].astype(int)
                xdiff = diffx_list[pos, 0:1]
                vdiff = diffv_list[pos, 0:1]
                rdiff = r_list[pos]
                x_data = torch.concatenate((xdiff, vdiff, rdiff[:, None]), axis=1)
                y_data = lin_edge_out_list[pos, 0:1]
                xdiff = diffx_list[pos, 1:2]
                vdiff = diffv_list[pos, 1:2]
                rdiff = r_list[pos]
                tmp = torch.concatenate((xdiff, vdiff, rdiff[:, None]), axis=1)
                x_data = torch.cat((x_data, tmp), dim=0)
                tmp = lin_edge_out_list[pos, 1:2]
                y_data = torch.cat((y_data, tmp), dim=0)
                model_pysrr, max_index, max_value = symbolic_regression_multi(x_data, y_data)

        for loop in range(2):
            for n in indexes:
                pos = np.argwhere(type_list == n)
                pos = pos[:, 0].astype(int)
                xdiff = to_numpy(diffx_list[pos, :])
                vdiff = to_numpy(diffv_list[pos, :])
                rdiff = to_numpy(r_list[pos])
                x_data = np.concatenate((xdiff, vdiff, rdiff[:, None]), axis=1)
                y_data = to_numpy(torch.norm(lin_edge_out_list[pos, :], dim=1))
                if loop == 0:
                    lin_fit, lin_fitv = curve_fit(boids_model, x_data, y_data, method='dogbox')
                else:
                    lin_fit, lin_fitv = curve_fit(boids_model, x_data, y_data, method='dogbox', p0=p00)
                cohesion_fit[int(n)] = lin_fit[0]
                alignment_fit[int(n)] = lin_fit[1]
                separation_fit[int(n)] = lin_fit[2]
            p00 = [np.mean(cohesion_fit[indexes]), np.mean(alignment_fit[indexes]), np.mean(separation_fit[indexes])]

        threshold = 0.25

        x_data = np.abs(to_numpy(p[:, 0]) * 0.5E-5)
        y_data = np.abs(cohesion_fit)
        x_data = x_data[indexes]
        y_data = y_data[indexes]
        lin_fit, r_squared, relative_error, outliers, x_data, y_data = linear_fit(x_data, y_data, threshold)

        fig, ax = fig_init()
        fmt = lambda x, pos: '{:.1f}e-4'.format((x) * 1e4, pos)
        ax.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        for id, n in enumerate(indexes):
            plt.scatter(x_data[id], y_data[id], color=cmap.color(n), s=400)
        plt.xlabel(r'True cohesion coeff. ', fontsize=56)
        plt.ylabel(r'Reconstructed cohesion coeff. ', fontsize=56)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/cohesion_{config_file}_{epoch}.tif", dpi=300)
        plt.close()
        logger.info(f'cohesion slope: {np.round(lin_fit[0], 2)}  R^2$: {np.round(r_squared, 3)}  outliers: {np.sum(relative_error > threshold)} ')

        x_data = np.abs(to_numpy(p[:, 1]) * 5E-4)
        y_data = alignment_fit
        x_data = x_data[indexes]
        y_data = y_data[indexes]
        lin_fit, r_squared, relative_error, outliers, x_data, y_data = linear_fit(x_data, y_data, threshold)

        fig, ax = fig_init()
        fmt = lambda x, pos: '{:.1f}e-2'.format((x) * 1e2, pos)
        ax.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        for id, n in enumerate(indexes):
            plt.scatter(x_data[id], y_data[id], color=cmap.color(n), s=400)
        plt.xlabel(r'True alignement coeff. ', fontsize=56)
        plt.ylabel(r'Reconstructed alignement coeff. ', fontsize=56)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/alignment_{config_file}_{epoch}.tif", dpi=300)
        plt.close()
        logger.info(f'alignment   slope: {np.round(lin_fit[0], 2)}  R^2$: {np.round(r_squared, 3)}  outliers: {np.sum(relative_error > threshold)} ')

        x_data = np.abs(to_numpy(p[:, 2]) * 1E-8)
        y_data = separation_fit
        x_data = x_data[indexes]
        y_data = y_data[indexes]
        lin_fit, r_squared, relative_error, outliers, x_data, y_data = linear_fit(x_data, y_data, threshold)

        fig, ax = fig_init()
        fmt = lambda x, pos: '{:.1f}e-7'.format((x) * 1e7, pos)
        ax.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        for id, n in enumerate(indexes):
            plt.scatter(x_data[id], y_data[id], color=cmap.color(n), s=400)
        plt.xlabel(r'True separation coeff. ', fontsize=56)
        plt.ylabel(r'Reconstructed separation coeff. ', fontsize=56)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/separation_{config_file}_{epoch}.tif", dpi=300)
        plt.close()
        logger.info(f'separation   slope: {np.round(lin_fit[0], 2)}  R^2$: {np.round(r_squared, 3)}  outliers: {np.sum(relative_error > threshold)} ')

        print('compare reconstructed interaction with ground truth...')
        fig, ax = fig_init()
        rr = torch.tensor(np.linspace(-max_radius, max_radius, 1000)).to(device)
        func_list = []
        true_func_list = []
        x = x_list[0][-1].clone().detach()
        for n in np.arange(len(x)):
            embedding_ = model.a[1, n, :] * torch.ones((1000, config.graph_model.embedding_dim), device=device)
            in_features = torch.cat((rr[:, None] / max_radius, 0 * rr[:, None],
                                     torch.abs(rr[:, None]) / max_radius, 0 * rr[:, None], 0 * rr[:, None],
                                     0 * rr[:, None], 0 * rr[:, None], embedding_), dim=1)
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
            func = func[:, 0]
            type = to_numpy(x[n, 5]).astype(int)
            if type < n_particle_types:
                func_list.append(func)
                true_func = model_B.psi(rr, p[type])
                true_func_list.append(true_func)
                if (n % 10 == 0):
                    plt.plot(to_numpy(rr),
                             to_numpy(func) * to_numpy(ynorm),
                             color=cmap.color(type), linewidth=4, alpha=0.25)
        func_list = torch.stack(func_list)
        true_func_list = torch.stack(true_func_list)
        plt.ylim([-1E-4, 1E-4])
        plt.xlabel(r'$x_j-x_i$', fontsize=64)
        plt.ylabel(r'$f_{ij}$', fontsize=64)
        ax.xaxis.set_major_locator(plt.MaxNLocator(3))
        ax.yaxis.set_major_locator(plt.MaxNLocator(5))
        ax.xaxis.set_major_formatter(FormatStrFormatter('%.2f'))
        fmt = lambda x, pos: '{:.1f}e-5'.format((x) * 1e5, pos)
        ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/func_dij_{config_file}_{epoch}.tif", dpi=300)
        plt.close()

        fig, ax = fig_init()
        for n in range(n_particle_types):
            true_func = model_B.psi(rr, p[n])
            plt.plot(to_numpy(rr), to_numpy(true_func), color=cmap.color(n), linewidth=4)
        plt.ylim([-1E-4, 1E-4])
        plt.xlabel(r'$x_j-x_i$', fontsize=64)
        plt.ylabel(r'$f_{ij}$', fontsize=64)
        ax.xaxis.set_major_locator(plt.MaxNLocator(3))
        ax.yaxis.set_major_locator(plt.MaxNLocator(5))
        ax.xaxis.set_major_formatter(FormatStrFormatter('%.2f'))
        fmt = lambda x, pos: '{:.1f}e-5'.format((x) * 1e5, pos)
        ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_func_dij_{config_file}_{epoch}.tif", dpi=300)
        func_list = func_list * ynorm
        func_list_ = torch.clamp(func_list, min=torch.tensor(-1.0E-4, device=device),
                                 max=torch.tensor(1.0E-4, device=device))
        true_func_list_ = torch.clamp(true_func_list, min=torch.tensor(-1.0E-4, device=device),
                                      max=torch.tensor(1.0E-4, device=device))
        rmserr_list = torch.sqrt(torch.mean((func_list_ - true_func_list_) ** 2, 1))
        rmserr_list = to_numpy(rmserr_list)
        print("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))
        logger.info("all function RMS error: {:.1e}+/-{:.1e}".format(np.mean(rmserr_list), np.std(rmserr_list)))


def data_plot_wave(config_file, epoch_list, log_dir, logger, cc, device):
    # Load parameters from config file
    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_nodes = config.simulation.n_nodes
    n_nodes_per_axis = int(np.sqrt(n_nodes))
    n_node_types = config.simulation.n_node_types
    n_frames = config.simulation.n_frames
    node_value_map = config.simulation.node_value_map
    n_runs = config.training.n_runs
    embedding_cluster = EmbeddingCluster(config)
    cmap = CustomColorMap(config=config)
    node_type_map = config.simulation.node_type_map
    has_pic = 'pics' in config.simulation.node_type_map

    vnorm = torch.tensor(1.0, device=device)
    ynorm = torch.tensor(1.0, device=device)
    hnorm = torch.load(f'./log/try_{config_file}/hnorm.pt', map_location=device).to(device)

    x_mesh_list = []
    y_mesh_list = []
    time.sleep(0.5)
    for run in trange(n_runs):
        x_mesh = torch.load(f'graphs_data/graphs_{dataset_name}/x_mesh_list_{run}.pt', map_location=device)
        x_mesh_list.append(x_mesh)
        h = torch.load(f'graphs_data/graphs_{dataset_name}/y_mesh_list_{run}.pt', map_location=device)
        y_mesh_list.append(h)
    h = y_mesh_list[0][0].clone().detach()

    print(f'hnorm: {to_numpy(hnorm)}')
    time.sleep(0.5)
    mesh_data = torch.load(f'graphs_data/graphs_{dataset_name}/mesh_data_1.pt', map_location=device)
    mask_mesh = mesh_data['mask']
    edge_index_mesh = mesh_data['edge_index']
    edge_weight_mesh = mesh_data['edge_weight']

    x_mesh = x_mesh_list[0][n_frames - 1].clone().detach()
    type_list = x_mesh[:, 5:6].clone().detach()
    n_nodes = x_mesh.shape[0]
    print(f'N nodes: {n_nodes}')

    index_nodes = []
    x_mesh = x_mesh_list[1][0].clone().detach()
    for n in range(n_node_types):
        index = np.argwhere(x_mesh[:, 5].detach().cpu().numpy() == n)
        index_nodes.append(index.squeeze())

    # plt.rcParams['text.usetex'] = True
    # rc('font', **{'family': 'serif', 'serif': ['Palatino']})
    # matplotlib.use("Qt5Agg")

    if has_pic:
        i0 = imread(f'graphs_data/{config.simulation.node_type_map}')
        coeff = i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(int)] / 255
        coeff_ = coeff
        coeff = np.reshape(coeff, (n_nodes_per_axis, n_nodes_per_axis))
        coeff = np.flipud(coeff) * config.simulation.beta
    else:
        c = initialize_random_values(n_node_types, device)
        for n in range(n_node_types):
            c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
        c = to_numpy(c)
        i0 = imread(f'graphs_data/{node_type_map}')
        values = i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(int)]
        features_mesh = values
        coeff = c[features_mesh]
        coeff = np.reshape(coeff, (n_nodes_per_axis, n_nodes_per_axis)) * config.simulation.beta
        coeff = np.flipud(coeff)
    vm = np.max(coeff)

    fig, ax = fig_init()
    fmt = lambda x, pos: '{:.1f}'.format((x) / 100, pos)
    axf.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
    axf.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
    plt.imshow(coeff, cmap=cc, vmin=0, vmax=vm)
    plt.xlabel(r'$x$', fontsize=64)
    plt.ylabel(r'$y$', fontsize=64)
    # cbar = plt.colorbar(shrink=0.5)
    # cbar.ax.tick_params(labelsize=32)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/true_wave_coeff_{config_file}.tif", dpi=300)

    net_list = ['0_1000', '0_2000', '0_5000', '1', '5', '20']

    net_list = glob.glob(f"./log/try_{config_file}/models/*.pt")

    for net in net_list:

        # net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_{net_}.pt"
        net_ = net.split('graphs')[1]

        mesh_model, bc_pos, bc_dpos = choose_training_model(config, device)
        state_dict = torch.load(net, map_location=device)
        mesh_model.load_state_dict(state_dict['model_state_dict'])
        mesh_model.eval()

        embedding = get_embedding(mesh_model.a, 1)

        fig, ax = fig_init()
        if has_pic:
            plt.scatter(embedding[:, 0], embedding[:, 1],
                        color=cmap.color(np.round(coeff_ * 256).astype(int)), s=100, alpha=1)
        else:
            for n in range(n_node_types):
                c_ = np.round(n / (n_node_types - 1) * 256).astype(int)
                plt.scatter(embedding[index_nodes[n], 0], embedding[index_nodes[n], 1], c=cmap.color(c_), s=200,
                            alpha=1)
        plt.xlabel(r'$a_{i0}$', fontsize=32)
        plt.ylabel(r'$a_{i1}$', fontsize=32)
        # plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        # plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        rr = torch.tensor(np.linspace(-150, 150, 200)).to(device)
        popt_list = []
        func_list = []
        for n in range(n_nodes):
            embedding_ = mesh_model.a[1, n, :] * torch.ones((200, 2), device=device)
            in_features = torch.cat((rr[:, None], embedding_), dim=1)
            h = mesh_model.lin_phi(in_features.float())
            h = h[:, 0]
            popt, pcov = curve_fit(linear_model, to_numpy(rr.squeeze()), to_numpy(h.squeeze()))
            popt_list.append(popt)
            func_list.append(h)
        func_list = torch.stack(func_list)
        popt_list = np.array(popt_list)

        t = np.array(popt_list) * to_numpy(hnorm)
        t = t[:, 0]
        t = np.reshape(t, (n_nodes_per_axis, n_nodes_per_axis))
        t = np.flipud(t)

        fig, ax = fig_init()
        fmt = lambda x, pos: '{:.1f}'.format((x) / 100, pos)
        axf.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        axf.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
        plt.imshow(t, cmap=cc, vmin=0, vmax=vm)
        # plt.xlabel(r'$x$', fontsize=64)
        # plt.ylabel(r'$y$', fontsize=64)
        plt.xlabel('x', fontsize=32)
        plt.ylabel('y', fontsize=32)
        fmt = lambda x, pos: '{:.3%}'.format(x)
        # cbar = plt.colorbar(format=FuncFormatter(fmt),shrink=0.5)
        # cbar.ax.tick_params(labelsize=32)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/wave_coeff_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        if not (has_pic):
            proj_interaction = popt_list
            proj_interaction[:, 1] = proj_interaction[:, 0]
            match config.training.cluster_method:
                case 'kmeans_auto_plot':
                    labels, n_clusters = embedding_cluster.get(proj_interaction, 'kmeans_auto')
                case 'kmeans_auto_embedding':
                    labels, n_clusters = embedding_cluster.get(embedding, 'kmeans_auto')
                    proj_interaction = embedding
                case 'distance_plot':
                    labels, n_clusters = embedding_cluster.get(proj_interaction, 'distance')
                case 'distance_embedding':
                    labels, n_clusters = embedding_cluster.get(embedding, 'distance', thresh=1.5)
                    proj_interaction = embedding
                case 'distance_both':
                    new_projection = np.concatenate((proj_interaction, embedding), axis=-1)
                    labels, n_clusters = embedding_cluster.get(new_projection, 'distance')

            label_list = []
            for n in range(n_node_types):
                tmp = labels[index_nodes[n]]
                label_list.append(np.round(np.median(tmp)))
            label_list = np.array(label_list)
            new_labels = labels.copy()
            for n in range(n_node_types):
                new_labels[labels == label_list[n]] = n
            accuracy = metrics.accuracy_score(to_numpy(type_list), new_labels)

            print(f'accuracy: {accuracy}  n_clusters: {n_clusters}')


def data_plot_particle_field(config_file, epoch_list, log_dir, logger, cc, device):

    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    dimension = config.simulation.dimension
    max_radius = config.simulation.max_radius
    n_particle_types = config.simulation.n_particle_types
    n_particles = config.simulation.n_particles
    n_nodes = config.simulation.n_nodes
    n_node_types = config.simulation.n_node_types
    node_type_map = config.simulation.node_type_map
    node_value_map = config.simulation.node_value_map
    has_video = 'video' in node_value_map
    n_nodes_per_axis = int(np.sqrt(n_nodes))
    n_frames = config.simulation.n_frames
    has_siren = 'siren' in config.graph_model.field_type
    has_siren_time = 'siren_with_time' in config.graph_model.field_type
    target_batch_size = config.training.batch_size
    has_ghost = config.training.n_ghosts > 0
    if config.training.small_init_batch_size:
        get_batch_size = increasing_batch_size(target_batch_size)
    else:
        get_batch_size = constant_batch_size(target_batch_size)
    batch_size = get_batch_size(0)
    cmap = CustomColorMap(config=config)  # create colormap for given config.graph_model
    embedding_cluster = EmbeddingCluster(config)
    n_runs = config.training.n_runs

    x_list = []
    y_list = []
    x_list.append(torch.load(f'graphs_data/graphs_{dataset_name}/x_list_0.pt', map_location=device))
    y_list.append(torch.load(f'graphs_data/graphs_{dataset_name}/y_list_0.pt', map_location=device))
    ynorm = torch.load(f'./log/try_{dataset_name}/ynorm.pt', map_location=device).to(device)
    vnorm = torch.load(f'./log/try_{dataset_name}/vnorm.pt', map_location=device).to(device)

    x_mesh_list = []
    y_mesh_list = []
    x_mesh = torch.load(f'graphs_data/graphs_{dataset_name}/x_mesh_list_0.pt', map_location=device)
    x_mesh_list.append(x_mesh)
    y_mesh = torch.load(f'graphs_data/graphs_{dataset_name}/y_mesh_list_0.pt', map_location=device)
    y_mesh_list.append(y_mesh)
    hnorm = torch.load(f'./log/try_{dataset_name}/hnorm.pt', map_location=device).to(device)

    mesh_data = torch.load(f'graphs_data/graphs_{dataset_name}/mesh_data_0.pt', map_location=device)
    mask_mesh = mesh_data['mask']
    mask_mesh = mask_mesh.repeat(batch_size, 1)

    # matplotlib.use("Qt5Agg")
    # plt.rcParams['text.usetex'] = True
    # rc('font', **{'family': 'serif', 'serif': ['Palatino']})

    x_mesh = x_mesh_list[0][0].clone().detach()
    i0 = imread(f'graphs_data/{node_value_map}')
    if has_video:
        i0 = i0[0]
        target = i0[(to_numpy(x_mesh[:, 2]) * 100).astype(int), (to_numpy(x_mesh[:, 1]) * 100).astype(int)]
        target = np.reshape(target, (n_nodes_per_axis, n_nodes_per_axis))
    else:
        target = i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(int)]
        target = np.reshape(target, (n_nodes_per_axis, n_nodes_per_axis))
        target = np.flipud(target)
    vm = np.max(target)
    if vm == 0:
        vm = 0.01

    fig, ax = fig_init()
    plt.imshow(target, cmap=cc, vmin=0, vmax=vm)
    plt.xlabel(r'$x$', fontsize=64)
    plt.ylabel(r'$y$', fontsize=64)
    cbar = plt.colorbar(shrink=0.5)
    cbar.ax.tick_params(labelsize=32)
    # cbar.set_label(r'$Coupling$',fontsize=64)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/target_field.tif", dpi=300)
    plt.close()

    print('Create models ...')
    model, bc_pos, bc_dpos = choose_training_model(config, device)

    x = x_list[0][0].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)
    if has_ghost:
        ghosts_particles = Ghost_Particles(config, n_particles, device)
        if config.training.ghost_method == 'MLP':
            optimizer_ghost_particles = torch.optim.Adam([ghosts_particles.data], lr=5E-4)
        else:
            optimizer_ghost_particles = torch.optim.Adam([ghosts_particles.ghost_pos], lr=1E-4)
        mask_ghost = np.concatenate((np.ones(n_particles), np.zeros(config.training.n_ghosts)))
        mask_ghost = np.tile(mask_ghost, batch_size)
        mask_ghost = np.argwhere(mask_ghost == 1)
        mask_ghost = mask_ghost[:, 0].astype(int)
    index_nodes = []
    x_mesh = x_mesh_list[0][0].clone().detach()
    for n in range(n_node_types):
        index = np.argwhere(x_mesh[:, 5].detach().cpu().numpy() == -n - 1)
        index_nodes.append(index.squeeze())

    if has_siren:

        image_width = int(np.sqrt(n_nodes))
        if has_siren_time:
            model_f = Siren_Network(image_width=image_width, in_features=3, out_features=1, hidden_features=128,
                                    hidden_layers=5, outermost_linear=True, device=device, first_omega_0=80,
                                    hidden_omega_0=80.)
        else:
            model_f = Siren_Network(image_width=image_width, in_features=2, out_features=1, hidden_features=64,
                                    hidden_layers=3, outermost_linear=True, device=device, first_omega_0=80,
                                    hidden_omega_0=80.)
        model_f.to(device=device)
        model_f.eval()

    epoch_list = [20]
    for epoch in epoch_list:
        print(f'epoch: {epoch}')

        net = f"./log/try_{config_file}/models/best_model_with_1_graphs_{epoch}.pt"
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])

        if has_siren:
            net = f'./log/try_{config_file}/models/best_model_f_with_1_graphs_{epoch}.pt'
            state_dict = torch.load(net, map_location=device)
            model_f.load_state_dict(state_dict['model_state_dict'])

        embedding = get_embedding(model.a, 1)

        fig, ax = fig_init()
        csv_ = []
        for n in range(n_particle_types):
            plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], color=cmap.color(n), s=400,
                        alpha=0.1)
            csv_.append(embedding[index_particles[n], :])
        # plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        # plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        # csv_ = np.array(csv_)
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{epoch}.tif", dpi=300)
        # np.save(f"./{log_dir}/results/embedding_{config_file}.npy", csv_)
        # csv_= np.reshape(csv_,(csv_.shape[0]*csv_.shape[1],2))
        # np.savetxt(f"./{log_dir}/results/embedding_{config_file}.txt", csv_)
        plt.close()

        rr = torch.tensor(np.linspace(0, max_radius, 1000)).to(device)
        func_list, proj_interaction = analyze_edge_function(rr=rr, vizualize=False, config=config,
                                                            model_lin_edge=model.lin_edge, model_a=model.a,
                                                            dataset_number=1,
                                                            n_particles=int(
                                                                n_particles * (1 - config.training.particle_dropout)),
                                                            ynorm=ynorm,
                                                            types=to_numpy(x[:, 5]),
                                                            cmap=cmap, device=device)

        match config.training.cluster_method:
            case 'kmeans':
                labels, n_clusters = embedding_cluster.get(proj_interaction, 'kmeans')
            case 'kmeans_auto_plot':
                labels, n_clusters = embedding_cluster.get(proj_interaction, 'kmeans_auto')
            case 'kmeans_auto_embedding':
                labels, n_clusters = embedding_cluster.get(embedding, 'kmeans_auto')
                proj_interaction = embedding
            case 'distance_plot':
                labels, n_clusters = embedding_cluster.get(proj_interaction, 'distance')
            case 'distance_embedding':
                labels, n_clusters = embedding_cluster.get(embedding, 'distance', thresh=0.05)
                proj_interaction = embedding
            case 'distance_both':
                new_projection = np.concatenate((proj_interaction, embedding), axis=-1)
                labels, n_clusters = embedding_cluster.get(new_projection, 'distance')

        fig, ax = fig_init()
        axf.yaxis.set_major_formatter(FormatStrFormatter('%.1f'))
        for n in range(n_clusters):
            pos = np.argwhere(labels == n)
            pos = np.array(pos)
            if pos.size > 0:
                print(f'cluster {n}  {len(pos)}')
                plt.scatter(proj_interaction[pos, 0], proj_interaction[pos, 1], color=cmap.color(n), s=100, alpha=0.1)
        label_list = []
        for n in range(n_particle_types):
            tmp = labels[index_particles[n]]
            label_list.append(np.round(np.median(tmp)))
        label_list = np.array(label_list)
        plt.xlabel(r'UMAP-proj 0', fontsize=64)
        plt.ylabel(r'UMAP-proj 1', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/UMAP_{config_file}_{epoch}.tif", dpi=300)
        plt.close()

        fig, ax = fig_init()
        new_labels = labels.copy()
        for n in range(n_particle_types):
            new_labels[labels == label_list[n]] = n
            pos = np.argwhere(labels == label_list[n])
            pos = np.array(pos)
            if pos.size > 0:
                plt.scatter(proj_interaction[pos, 0], proj_interaction[pos, 1],
                            color=cmap.color(n), s=0.1)
        type_list = x[:, 5:6].clone().detach()
        accuracy = metrics.accuracy_score(to_numpy(type_list), new_labels)
        print(f'accuracy: {np.round(accuracy, 2)}   n_clusters: {n_clusters}')
        plt.close

        p = config.simulation.params
        if len(p) > 1:
            p = torch.tensor(p, device=device)
        else:
            p = torch.load(f'graphs_data/graphs_{dataset_name}/model_p.pt', map_location=device)
        model_a_first = model.a.clone().detach()

        match config.graph_model.field_type:

            case 'siren_with_time' | 'siren':

                s_p = 100

                if has_video:

                    x_mesh = x_mesh_list[0][0].clone().detach()
                    i0 = imread(f'graphs_data/{node_value_map}')

                    os.makedirs(f"./{log_dir}/results/video", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/video/generated1", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/video/generated2", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/video/target", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/video/field", exist_ok=True)

                    print('Output per frame ...')

                    RMSE_list = []
                    PSNR_list = []
                    SSIM_list = []
                    for frame in trange(0, n_frames):
                        x = x_list[0][frame].clone().detach()
                        fig, ax = fig_init()
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        for n in range(n_particle_types):
                            plt.scatter(to_numpy(x[index_particles[n], 2]), 1 - to_numpy(x[index_particles[n], 1]),
                                        s=s_p,
                                        color='k')
                        plt.xlim([0, 1])
                        plt.ylim([0, 1])
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/video/generated1/generated_1_{epoch}_{frame}.tif",
                                    dpi=150)
                        plt.close()

                        fig, ax = fig_init()
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        for n in range(n_particle_types):
                            plt.scatter(to_numpy(x[index_particles[n], 2]), 1 - to_numpy(x[index_particles[n], 1]),
                                        s=s_p)
                        plt.xlim([0, 1])
                        plt.ylim([0, 1])
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/video/generated2/generated_2_{epoch}_{frame}.tif",
                                    dpi=150)
                        plt.close()

                        i0_ = i0[frame]
                        y = i0_[(to_numpy(x_mesh[:, 2]) * 100).astype(int), (to_numpy(x_mesh[:, 1]) * 100).astype(int)]
                        y = np.reshape(y, (n_nodes_per_axis, n_nodes_per_axis))
                        fig, ax = fig_init()
                        plt.imshow(y, cmap=cc, vmin=0, vmax=vm)
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/video/target/target_field_{epoch}_{frame}.tif",
                                    dpi=150)
                        plt.close()

                        pred = model_f(time=frame / n_frames) ** 2
                        pred = torch.reshape(pred, (n_nodes_per_axis, n_nodes_per_axis))
                        pred = to_numpy(torch.sqrt(pred))
                        pred = np.flipud(pred)
                        fig, ax = fig_init()
                        pred = np.rot90(pred)
                        pred = np.fliplr(pred)
                        plt.imshow(pred, cmap=cc, vmin=0, vmax=1)
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/video/field/reconstructed_field_{epoch}_{frame}.tif",
                                    dpi=150)
                        plt.close()

                        RMSE = np.sqrt(np.mean((y - pred) ** 2))
                        RMSE_list = np.concatenate((RMSE_list, [RMSE]))
                        PSNR = calculate_psnr(y, pred, max_value=np.max(y))
                        PSNR_list = np.concatenate((PSNR_list, [PSNR]))
                        SSIM = calculate_ssim(y, pred)
                        SSIM_list = np.concatenate((SSIM_list, [SSIM]))

                else:

                    x_mesh = x_mesh_list[0][0].clone().detach()
                    node_value_map = config.simulation.node_value_map
                    n_nodes_per_axis = int(np.sqrt(n_nodes))
                    i0 = imread(f'graphs_data/{node_value_map}')
                    target = i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(
                        int)] * 5000 / 255
                    target = np.reshape(target, (n_nodes_per_axis, n_nodes_per_axis))
                    target = np.flipud(target)

                    os.makedirs(f"./{log_dir}/results/rotation", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/rotation/generated1", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/rotation/generated2", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/rotation/target", exist_ok=True)
                    os.makedirs(f"./{log_dir}/results/rotation/field", exist_ok=True)

                    match config.graph_model.field_type:
                        case 'siren':
                            angle_list = [0]
                        case 'siren_with_time':
                            angle_list = trange(0, n_frames, 5)
                    print('Output per angle ...')

                    RMSE_list = []
                    PSNR_list = []
                    SSIM_list = []
                    for angle in angle_list:

                        x = x_list[0][angle].clone().detach()
                        fig, ax = fig_init()
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        for n in range(n_particle_types):
                            plt.scatter(to_numpy(x[index_particles[n], 1]), to_numpy(x[index_particles[n], 2]), s=s_p,
                                        color='k')
                        plt.xlim([0, 1])
                        plt.ylim([0, 1])
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/rotation/generated1/generated_1_{epoch}_{angle}.tif", dpi=150)
                        plt.close()

                        fig, ax = fig_init()
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        for n in range(n_particle_types):
                            plt.scatter(to_numpy(x[index_particles[n], 1]), to_numpy(x[index_particles[n], 2]), s=s_p)
                        plt.xlim([0, 1])
                        plt.ylim([0, 1])
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/rotation/generated2/generated_2_{epoch}_{angle}.tif", dpi=150)
                        plt.close()

                        fig, ax = fig_init()
                        y = ndimage.rotate(target, -angle, reshape=False, cval=np.mean(target) * 1.1)
                        plt.imshow(y, cmap=cc, vmin=0, vmax=vm)
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/rotation/target/target_field_{epoch}_{angle}.tif", dpi=150)
                        plt.close()

                        match config.graph_model.field_type:
                            case 'siren':
                                pred = model_f() ** 2
                            case 'siren_with_time':
                                pred = model_f(time=angle / n_frames) ** 2
                        pred = torch.reshape(pred, (n_nodes_per_axis, n_nodes_per_axis))
                        pred = to_numpy(torch.sqrt(pred))
                        pred = np.flipud(pred)

                        fig, ax = fig_init()
                        plt.imshow(pred, cmap=cc, vmin=0, vmax=vm)
                        plt.xlabel(r'$x$', fontsize=64)
                        plt.ylabel(r'$y$', fontsize=64)
                        plt.tight_layout()
                        plt.savefig(f"./{log_dir}/results/rotation/field/reconstructed_field_{epoch}_{angle}.tif",
                                    dpi=150)
                        plt.close()

                        RMSE = np.sqrt(np.mean((y - pred) ** 2))
                        RMSE_list = np.concatenate((RMSE_list, [RMSE]))
                        PSNR = calculate_psnr(y, pred, max_value=np.max(y))
                        PSNR_list = np.concatenate((PSNR_list, [PSNR]))
                        SSIM = calculate_ssim(y, pred)
                        SSIM_list = np.concatenate((SSIM_list, [SSIM]))

                fig, ax = fig_init()
                plt.scatter(np.linspace(0, n_frames, len(SSIM_list)), SSIM_list, color='k', linewidth=4)
                plt.xlabel(r'$Frame$', fontsize=64)
                plt.ylabel(r'$SSIM$', fontsize=64)
                plt.ylim([0, 1])
                plt.tight_layout()
                plt.savefig(f"./{log_dir}/results/ssim_{epoch}.tif", dpi=150)
                plt.close()

                print(f'SSIM: {np.round(np.mean(SSIM_list), 3)}+/-{np.round(np.std(SSIM_list), 3)}')

                fig, ax = fig_init()
                plt.scatter(np.linspace(0, n_frames, len(SSIM_list)), RMSE_list, color='k', linewidth=4)
                plt.xlabel(r'$Frame$', fontsize=64)
                plt.ylabel(r'RMSE', fontsize=64)
                plt.ylim([0, 1])
                plt.tight_layout()
                plt.savefig(f"./{log_dir}/results/rmse_{epoch}.tif", dpi=150)
                plt.close()

                fig, ax = fig_init()
                plt.scatter(np.linspace(0, n_frames, len(SSIM_list)), PSNR_list, color='k', linewidth=4)
                plt.xlabel(r'$Frame$', fontsize=64)
                plt.ylabel(r'PSNR', fontsize=64)
                plt.ylim([0, 50])
                plt.tight_layout()
                plt.savefig(f"./{log_dir}/results/psnr_{epoch}.tif", dpi=150)
                plt.close()

            case 'tensor':

                fig, ax = fig_init()
                pts = to_numpy(torch.reshape(model.field[1], (100, 100)))
                pts = np.flipud(pts)
                plt.imshow(pts, cmap=cc, vmin=0, vmax=vm)
                plt.xlabel(r'$x$', fontsize=64)
                plt.ylabel(r'$y$', fontsize=64)
                cbar = plt.colorbar(shrink=0.5)
                cbar.ax.tick_params(labelsize=32)
                # cbar.set_label(r'$Coupling$',fontsize=64)
                plt.tight_layout()
                imsave(f"./{log_dir}/results/field_pic_{config_file}_{epoch}.tif", pts)
                plt.savefig(f"./{log_dir}/results/field_{config_file}_{epoch}.tif", dpi=300)
                # np.save(f"./{log_dir}/results/embedding_{config_file}.npy", csv_)
                # csv_= np.reshape(csv_,(csv_.shape[0]*csv_.shape[1],2))
                # np.savetxt(f"./{log_dir}/results/embedding_{config_file}.txt", csv_)
                plt.close()
                rmse = np.sqrt(np.mean((target - pts) ** 2))
                print(f'RMSE: {rmse}')

                fig, ax = fig_init()
                plt.scatter(target, pts, c='k', s=100, alpha=0.1)
                plt.ylabel(r'Reconstructed coupling', fontsize=32)
                plt.xlabel(r'True coupling', fontsize=32)
                plt.xlim([-vm * 0.1, vm * 1.5])
                plt.ylim([-vm * 0.1, vm * 1.5])
                plt.tight_layout()
                plt.savefig(f"./{log_dir}/results/field_scatter_{config_file}_{epoch}.tif", dpi=300)

                x_data = np.reshape(pts, (n_nodes))
                y_data = np.reshape(target, (n_nodes))
                threshold = 0.25
                relative_error = np.abs(y_data - x_data)
                print(f'outliers: {np.sum(relative_error > threshold)} / {n_particles}')
                pos = np.argwhere(relative_error < threshold)
                pos_outliers = np.argwhere(relative_error > threshold)

                x_data_ = x_data[pos].squeeze()
                y_data_ = y_data[pos].squeeze()
                # x_data_ = x_data.squeeze()
                # y_data_ = y_data.squeeze()

                lin_fit, lin_fitv = curve_fit(linear_model, x_data_, y_data_)
                residuals = y_data_ - linear_model(x_data_, *lin_fit)
                ss_res = np.sum(residuals ** 2)
                ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
                r_squared = 1 - (ss_res / ss_tot)

                print(f'R^2$: {np.round(r_squared, 3)} ')
                print(f"Slope: {np.round(lin_fit[0], 2)}")

                plt.plot(x_data_, linear_model(x_data_, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
                plt.xlim([-vm * 0.1, vm * 1.1])
                plt.ylim([-vm * 0.1, vm * 1.1])
                plt.tight_layout()
                plt.savefig(f"./{log_dir}/results/field_scatter_{config_file}_{epoch}.tif", dpi=300)


def data_plot_RD(config_file, epoch_list, log_dir, logger, cc, device):
    # Load parameters from config file
    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_nodes = config.simulation.n_nodes
    n_nodes_per_axis = int(np.sqrt(n_nodes))
    n_node_types = config.simulation.n_node_types
    n_frames = config.simulation.n_frames
    n_runs = config.training.n_runs
    node_value_map = config.simulation.node_value_map
    aggr_type = config.graph_model.aggr_type
    delta_t = config.simulation.delta_t
    cmap = CustomColorMap(config=config)
    node_type_map = config.simulation.node_type_map
    has_pic = 'pics' in config.simulation.node_type_map

    embedding_cluster = EmbeddingCluster(config)

    vnorm = torch.tensor(1.0, device=device)
    ynorm = torch.tensor(1.0, device=device)
    hnorm = torch.load(f'./log/try_{config_file}/hnorm.pt', map_location=device).to(device)

    x_mesh_list = []
    y_mesh_list = []
    time.sleep(0.5)
    for run in trange(n_runs):
        x_mesh = torch.load(f'graphs_data/graphs_{dataset_name}/x_mesh_list_{run}.pt', map_location=device)
        x_mesh_list.append(x_mesh)
        h = torch.load(f'graphs_data/graphs_{dataset_name}/y_mesh_list_{run}.pt', map_location=device)
        y_mesh_list.append(h)
    h = y_mesh_list[0][0].clone().detach()

    print(f'hnorm: {to_numpy(hnorm)}')
    time.sleep(0.5)
    mesh_data = torch.load(f'graphs_data/graphs_{dataset_name}/mesh_data_1.pt', map_location=device)
    mask_mesh = mesh_data['mask']
    edge_index_mesh = mesh_data['edge_index']
    edge_weight_mesh = mesh_data['edge_weight']

    x_mesh = x_mesh_list[0][n_frames - 1].clone().detach()
    type_list = x_mesh[:, 5:6].clone().detach()
    n_nodes = x_mesh.shape[0]
    print(f'N nodes: {n_nodes}')

    index_nodes = []
    x_mesh = x_mesh_list[1][0].clone().detach()
    for n in range(n_node_types):
        index = np.argwhere(x_mesh[:, 5].detach().cpu().numpy() == n)
        index_nodes.append(index.squeeze())

    plt.rcParams['text.usetex'] = True
    rc('font', **{'family': 'serif', 'serif': ['Palatino']})
    matplotlib.use("Qt5Agg")

    if has_pic:
        i0 = imread(f'graphs_data/{config.simulation.node_type_map}')
        coeff = i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(int)]
        coeff_ = coeff
        coeff = np.reshape(coeff, (n_nodes_per_axis, n_nodes_per_axis))
        coeff = np.flipud(coeff) * config.simulation.beta
    else:
        c = initialize_random_values(n_node_types, device)
        for n in range(n_node_types):
            c[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
        c = to_numpy(c)
        i0 = imread(f'graphs_data/{node_type_map}')
        values = i0[(to_numpy(x_mesh[:, 1]) * 255).astype(int), (to_numpy(x_mesh[:, 2]) * 255).astype(int)]
        features_mesh = values
        coeff = c[features_mesh]
        coeff = np.reshape(coeff, (n_nodes_per_axis, n_nodes_per_axis)) * config.simulation.beta
        coeff = np.flipud(coeff)
        coeff = np.fliplr(coeff)
    vm = np.max(coeff)
    print(f'vm: {vm}')

    fig, ax = fig_init()
    fmt = lambda x, pos: '{:.1f}'.format((x) / 100, pos)
    axf.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
    axf.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
    plt.imshow(coeff, cmap=cc, vmin=0, vmax=vm)
    plt.xlabel(r'$x$', fontsize=64)
    plt.ylabel(r'$y$', fontsize=64)

    cbar = plt.colorbar(shrink=0.5)
    cbar.ax.tick_params(labelsize=32)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/true_coeff_{config_file}.tif", dpi=300)
    plt.close()

    net_list = ['20', '0_1000', '0_2000', '0_5000', '1', '5']

    for net_ in net_list:

        net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_{net_}.pt"
        model, bc_pos, bc_dpos = choose_training_model(config, device)
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        print(f'net: {net}')
        embedding = get_embedding(model.a, 1)
        first_embedding = embedding

        fig, ax = fig_init()
        if has_pic:
            plt.scatter(embedding[:, 0], embedding[:, 1],
                        color=cmap.color(np.round(coeff_ * 256).astype(int)), s=100, alpha=1)
        else:
            for n in range(n_node_types):
                c_ = np.round(n / (n_node_types - 1) * 256).astype(int)
                plt.scatter(embedding[index_nodes[n], 0], embedding[index_nodes[n], 1], s=200)  # , color=cmap.color(c_)
        plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        if not (has_pic):

            print('domain clustering...')
            labels, n_clusters = embedding_cluster.get(embedding, 'kmeans_auto')
            label_list = []
            for n in range(n_node_types):
                tmp = labels[index_nodes[n]]
                label_list.append(np.round(np.median(tmp)))
            label_list = np.array(label_list)
            new_labels = labels.copy()
            for n in range(n_node_types):
                new_labels[labels == label_list[n]] = n
            accuracy = metrics.accuracy_score(to_numpy(type_list), new_labels)
            print(f'accuracy: {accuracy}  n_clusters: {n_clusters}')

            model_a_ = model.a[1].clone().detach()
            for n in range(n_clusters):
                pos = np.argwhere(labels == n).squeeze().astype(int)
                pos = np.array(pos)
                if pos.size > 0:
                    median_center = model_a_[pos, :]
                    median_center = torch.median(median_center, dim=0).values
                    # plt.scatter(to_numpy(model_a_[pos, 0]), to_numpy(model_a_[pos, 1]), s=1, c='r', alpha=0.25)
                    model_a_[pos, :] = median_center
                    # plt.scatter(to_numpy(model_a_[pos, 0]), to_numpy(model_a_[pos, 1]), s=1, c='k')
            with torch.no_grad():
                model.a[1] = model_a_.clone().detach()

        print('fitting diffusion coeff with domain clustering...')

        if True:

            k = 2400

            # collect data from sing
            x_mesh = x_mesh_list[1][k].clone().detach()
            dataset = data.Data(x=x_mesh, edge_index=edge_index_mesh, edge_attr=edge_weight_mesh, device=device)
            with torch.no_grad():
                pred, laplacian_uvw, uvw, embedding, input_phi = model(dataset, data_id=1, return_all=True)
            pred = pred * hnorm
            y = y_mesh_list[1][k].clone().detach()

            # RD_RPS_model :
            c_ = torch.ones(n_node_types, 1, device=device) + torch.rand(n_node_types, 1, device=device)
            for n in range(n_node_types):
                c_[n] = torch.tensor(config.simulation.diffusion_coefficients[n])
            c = c_[to_numpy(dataset.x[:, 5])].squeeze()
            u = uvw[:, 0]
            v = uvw[:, 1]
            w = uvw[:, 2]
            # laplacian = mesh_model.beta * c * self.propagate(edge_index, x=(x, x), edge_attr=edge_attr)
            laplacian_u = c * laplacian_uvw[:, 0]
            laplacian_v = c * laplacian_uvw[:, 1]
            laplacian_w = c * laplacian_uvw[:, 2]
            a = 0.6
            p = u + v + w
            du = laplacian_u + u * (1 - p - a * v)
            dv = laplacian_v + v * (1 - p - a * w)
            dw = laplacian_w + w * (1 - p - a * u)
            increment = torch.cat((du[:, None], dv[:, None], dw[:, None]), dim=1)
            increment = increment.squeeze()

            lin_fit_true = np.zeros((np.max(new_labels) + 1, 3, 10))
            lin_fit_reconstructed = np.zeros((np.max(new_labels) + 1, 3, 10))
            eq_list = ['u', 'v', 'w']
            if has_pic:
                n_node_types_list = [0]
            else:
                n_node_types_list = np.arange(n_node_types)
            for n in np.unique(new_labels):
                if has_pic:
                    pos = np.argwhere(to_numpy(mask_mesh.squeeze()) == 1)
                else:
                    pos = np.argwhere((new_labels == n) & (to_numpy(mask_mesh.squeeze()) == 1))
                    pos = pos[:, 0].astype(int)

                for it, eq in enumerate(eq_list):
                    fitting_model = reaction_diffusion_model(eq)
                    laplacian_u = to_numpy(laplacian_uvw[pos, 0])
                    laplacian_v = to_numpy(laplacian_uvw[pos, 1])
                    laplacian_w = to_numpy(laplacian_uvw[pos, 2])
                    u = to_numpy(uvw[pos, 0])
                    v = to_numpy(uvw[pos, 1])
                    w = to_numpy(uvw[pos, 2])
                    x_data = np.concatenate((laplacian_u[:, None], laplacian_v[:, None], laplacian_w[:, None],
                                             u[:, None], v[:, None], w[:, None]), axis=1)
                    y_data = to_numpy(increment[pos, 0 + it:1 + it])
                    p0 = np.ones((10, 1))
                    lin_fit, lin_fitv = curve_fit(fitting_model, np.squeeze(x_data), np.squeeze(y_data),
                                                  p0=np.squeeze(p0), method='trf')
                    lin_fit_true[n, it] = lin_fit
                    y_data = to_numpy(pred[pos, it:it + 1])
                    lin_fit, lin_fitv = curve_fit(fitting_model, np.squeeze(x_data), np.squeeze(y_data),
                                                  p0=np.squeeze(p0), method='trf')
                    lin_fit_reconstructed[n, it] = lin_fit

            coeff_reconstructed = np.round(np.median(lin_fit_reconstructed, axis=0), 2)
            diffusion_coeff_reconstructed = np.round(np.median(lin_fit_reconstructed, axis=1), 2)[:, 9]
            coeff_true = np.round(np.median(lin_fit_true, axis=0), 2)
            diffusion_coeff_true = np.round(np.median(lin_fit_true, axis=1), 2)[:, 9]

            print(f'frame {k}')
            print(f'coeff_reconstructed: {coeff_reconstructed}')
            print(f'diffusion_coeff_reconstructed: {diffusion_coeff_reconstructed}')
            print(f'coeff_true: {coeff_true}')
            print(f'diffusion_coeff_true: {diffusion_coeff_true}')

            cp = ['uu', 'uv', 'uw', 'vv', 'vw', 'ww', 'u', 'v', 'w']
            results = {
                'True': coeff_true[0, 0:9],
                'Reconstructed': coeff_reconstructed[0, 0:9],
            }
            x = np.arange(len(cp))  # the label locations
            width = 0.25  # the width of the bars
            multiplier = 0
            fig, ax = fig_init()
            for attribute, measurement in results.items():
                offset = width * multiplier
                rects = ax.bar(x + offset, measurement, width, label=attribute)
                multiplier += 1
            ax.set_ylabel('Polynomial coefficient', fontsize=48)
            ax.set_xticks(x + width, cp, fontsize=48)
            plt.title('First equation', fontsize=48)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/first_equation_{config_file}_{net_}.tif", dpi=300)
            plt.close()
            cp = ['uu', 'uv', 'uw', 'vv', 'vw', 'ww', 'u', 'v', 'w']
            results = {
                'True': coeff_true[1, 0:9],
                'Reconstructed': coeff_reconstructed[1, 0:9],
            }
            x = np.arange(len(cp))  # the label locations
            width = 0.25  # the width of the bars
            multiplier = 0
            fig, ax = fig_init()
            for attribute, measurement in results.items():
                offset = width * multiplier
                rects = ax.bar(x + offset, measurement, width, label=attribute)
                multiplier += 1
            ax.set_ylabel('Polynomial coefficient', fontsize=48)
            ax.set_xticks(x + width, cp, fontsize=48)
            plt.title('Second equation', fontsize=48)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/second_equation_{config_file}_{net_}.tif", dpi=300)
            plt.close()
            cp = ['uu', 'uv', 'uw', 'vv', 'vw', 'ww', 'u', 'v', 'w']
            results = {
                'True': coeff_true[2, 0:9],
                'Reconstructed': coeff_reconstructed[2, 0:9],
            }
            x = np.arange(len(cp))  # the label locations
            width = 0.25  # the width of the bars
            multiplier = 0
            fig, ax = fig_init()
            for attribute, measurement in results.items():
                offset = width * multiplier
                rects = ax.bar(x + offset, measurement, width, label=attribute)
                multiplier += 1
            ax.set_ylabel('Polynomial coefficient', fontsize=48)
            ax.set_xticks(x + width, cp, fontsize=48)
            plt.title('Third equation', fontsize=48)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/third_equation_{config_file}_{net_}.tif", dpi=300)
            plt.close()

            fig, ax = fig_init()
            t = diffusion_coeff_reconstructed[new_labels]
            t_ = np.reshape(t, (n_nodes_per_axis, n_nodes_per_axis))
            t_ = np.flipud(t_)
            t_ = np.fliplr(t_)
            fig_ = plt.figure(figsize=(12, 12))
            axf = fig_.add_subplot(1, 1, 1)
            axf.xaxis.set_major_locator(plt.MaxNLocator(3))
            axf.yaxis.set_major_locator(plt.MaxNLocator(3))
            fmt = lambda x, pos: '{:.1f}'.format((x) / 100, pos)
            axf.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            axf.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            plt.imshow(t_, cmap=cc, vmin=0, vmax=vm)
            plt.xlabel(r'$x$', fontsize=64)
            plt.ylabel(r'$y$', fontsize=64)
            fmt = lambda x, pos: '{:.3%}'.format(x)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/diff_coeff_map_{config_file}_{net_}.tif", dpi=300)
            plt.close()

            t_ = np.reshape(t, (n_nodes_per_axis * n_nodes_per_axis))

            fig, ax = fig_init()
            plt.scatter(first_embedding[:, 0], first_embedding[:, 1],
                        s=200, c=t_, cmap='viridis', alpha=0.5, vmin=0, vmax=vm)
            plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
            plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{net_}.tif", dpi=300)
            plt.close()

    bContinuous = False
    if bContinuous:
        laplacian_uvw_list = []
        uvw_list = []
        pred_list = []
        input_phi_list = []
        for k in trange(n_frames - 1):
            x_mesh = x_mesh_list[1][k].clone().detach()
            dataset = data.Data(x=x_mesh, edge_index=edge_index_mesh, edge_attr=edge_weight_mesh, device=device)
            with torch.no_grad():
                pred, laplacian_uvw, uvw, embedding, input_phi = model(dataset, data_id=1, return_all=True)
            pred = pred * hnorm
            pred_list.append(pred)
            laplacian_uvw_list.append(laplacian_uvw)
            uvw_list.append(uvw)
            input_phi_list.append(input_phi)

        laplacian_uvw_list = torch.stack(laplacian_uvw_list)
        uvw_list = torch.stack(uvw_list)
        pred_list = torch.stack(pred_list)

        print('Fit node level ...')
        t = np.zeros((n_nodes, 1))
        for n in trange(n_nodes):
            for it, eq in enumerate(eq_list[0]):
                fitting_model = reaction_diffusion_model(eq)
                laplacian_u = to_numpy(laplacian_uvw_list[:, n, 0].squeeze())
                laplacian_v = to_numpy(laplacian_uvw_list[:, n, 1].squeeze())
                laplacian_w = to_numpy(laplacian_uvw_list[:, n, 2].squeeze())
                u = to_numpy(uvw_list[:, n, 0].squeeze())
                v = to_numpy(uvw_list[:, n, 1].squeeze())
                w = to_numpy(uvw_list[:, n, 2].squeeze())
                x_data = np.concatenate((laplacian_u[:, None], laplacian_v[:, None], laplacian_w[:, None], u[:, None],
                                         v[:, None], w[:, None]), axis=1)
                y_data = to_numpy(pred_list[:, n, it:it + 1].squeeze())
                lin_fit, lin_fitv = curve_fit(fitting_model, np.squeeze(x_data), y_data, method='trf')
                t[n] = lin_fit[-1:]

                if ((n % 1000 == 0) | (n == n_nodes - 1)):
                    t_ = np.reshape(t, (n_nodes_per_axis, n_nodes_per_axis))
                    t_ = np.flipud(t_)
                    t_ = np.fliplr(t_)

                    fig, ax = fig_init()
                    fmt = lambda x, pos: '{:.1f}'.format((x) / 100, pos)
                    axf.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
                    axf.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
                    plt.imshow(t_ * to_numpy(hnorm), cmap=cc, vmin=0, vmax=1)
                    plt.xlabel(r'$x$', fontsize=64)
                    plt.ylabel(r'$y$', fontsize=64)

                    fmt = lambda x, pos: '{:.3%}'.format(x)
                    plt.tight_layout()
                    plt.savefig(f"./{log_dir}/results/diff_node_coeff_{config_file}_{net_}.tif", dpi=300)
                    plt.close()

        input_phi_list = torch.stack(input_phi_list)
        t = np.zeros((n_nodes, 1))
        for node in trange(n_nodes):
            gg = []
            for sample in range(100):
                k = 1 + np.random.randint(n_frames - 2)
                input = input_phi_list[k, node, :].clone().detach().squeeze()
                input.requires_grad = True
                L = model.lin_phi(input)[sample % 3]
                [g] = torch.autograd.grad(L, [input])
                gg.append(g[sample % 3])
            t[node] = to_numpy(torch.median(torch.stack(gg)))
            if ((node % 1000 == 0) | (node == n_nodes - 1)):
                t_ = np.reshape(t, (n_nodes_per_axis, n_nodes_per_axis))
                t_ = np.flipud(t_)
                t_ = np.fliplr(t_)

                fig, ax = fig_init()
                fmt = lambda x, pos: '{:.1f}'.format((x) / 100, pos)
                axf.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
                axf.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
                plt.imshow(t_ * to_numpy(hnorm), cmap=cc, vmin=0, vmax=vm)
                plt.xlabel(r'$x$', fontsize=64)
                plt.ylabel(r'$y$', fontsize=64)
                fmt = lambda x, pos: '{:.3%}'.format(x)
                plt.tight_layout()
                plt.savefig(f"./{log_dir}/results/diff_coeff_{config_file}_{net_}.tif", dpi=300)
                plt.close()

        fig_ = plt.figure(figsize=(12, 12))
        axf = fig_.add_subplot(1, 1, 1)

        pos = torch.argwhere(mask_mesh == 1)
        pos = to_numpy(pos[:, 0]).astype(int)
        x_data = np.reshape(coeff, (n_nodes))
        y_data = np.reshape(t_, (n_nodes))
        x_data = x_data.squeeze()
        y_data = y_data.squeeze()
        x_data = x_data[pos]
        y_data = y_data[pos]

        axf.xaxis.set_major_locator(plt.MaxNLocator(3))
        axf.yaxis.set_major_locator(plt.MaxNLocator(3))
        axf.xaxis.set_major_formatter(FormatStrFormatter('%.3f'))
        axf.yaxis.set_major_formatter(FormatStrFormatter('%.3f'))
        plt.scatter(x_data, y_data, c='k', s=100, alpha=0.01)
        plt.ylabel(r'Reconstructed diffusion coeff.', fontsize=48)
        plt.xlabel(r'True diffusion coeff.', fontsize=48)
        plt.xlim([0, vm * 1.1])
        plt.ylim([0, vm * 1.1])

        lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)
        residuals = y_data - linear_model(x_data, *lin_fit)
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)
        plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/scatter_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        print(f"R^2$: {np.round(r_squared, 3)}  Slope: {np.round(lin_fit[0], 2)}")


def data_plot_signal(config_file, epoch_list, log_dir, logger, cc, device):
    # Load parameters from config file
    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    min_radius = config.simulation.min_radius
    n_frames = config.simulation.n_frames
    n_runs = config.training.n_runs
    n_particle_types = config.simulation.n_particle_types
    aggr_type = config.graph_model.aggr_type
    delta_t = config.simulation.delta_t
    cmap = CustomColorMap(config=config)
    dimension = config.simulation.dimension

    embedding_cluster = EmbeddingCluster(config)

    n_runs = min(2, n_runs)

    x_list = []
    y_list = []
    for run in trange(n_runs):
        x = torch.load(f'graphs_data/graphs_{dataset_name}/x_list_{run}.pt', map_location=device)
        y = torch.load(f'graphs_data/graphs_{dataset_name}/y_list_{run}.pt', map_location=device)
        x_list.append(x)
        y_list.append(y)
    vnorm = torch.load(os.path.join(log_dir, 'vnorm.pt'))
    ynorm = torch.load(os.path.join(log_dir, 'ynorm.pt'))
    print(f'vnorm: {to_numpy(vnorm)}, ynorm: {to_numpy(ynorm)}')

    print('Update variables ...')
    x = x_list[1][n_frames - 1].clone().detach()
    index_particles = get_index_particles(x, n_particle_types, dimension)
    type_list = get_type_list(x, dimension)
    n_particles = x.shape[0]
    print(f'N particles: {n_particles}')
    config.simulation.n_particles = n_particles

    mat = scipy.io.loadmat(config.simulation.connectivity_file)
    adjacency = torch.tensor(mat['A'], device=device)
    adj_t = adjacency > 0
    edge_index = adj_t.nonzero().t().contiguous()
    gt_weight = to_numpy(adjacency[adj_t])
    norm_gt_weight = max(gt_weight)

    fig, ax = fig_init()
    plt.imshow(to_numpy(adjacency) / norm_gt_weight, cmap=cc, vmin=0, vmax=0.1)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/True_Aij_{config_file}.tif", dpi=300)
    plt.close()

    fig, ax = fig_init()
    plt.imshow(to_numpy(adjacency) / norm_gt_weight, cmap=cc, vmin=0, vmax=0.1)
    cbar = plt.colorbar(shrink=0.5)
    cbar.ax.tick_params(labelsize=32)
    plt.tight_layout()
    plt.savefig(f"./{log_dir}/results/True_Aij_bar_{config_file}.tif", dpi=300)
    plt.close()

    # plt.rcParams['text.usetex'] = True
    # rc('font', **{'family': 'serif', 'serif': ['Palatino']})
    # matplotlib.use("Qt5Agg")

    GT_model, bc_pos, bc_dpos = choose_model(config, device=device)

    net_list = ['20', '25', '30', '39']  # [,'1','5','10'] # , '0', '1', '5']
    # net_list = glob.glob(f"./log/try_{config_file}/models/*.pt")

    for net_ in net_list:

        net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_{net_}.pt"
        # net_ = net.split('graphs')[1]

        net = f"./log/try_{config_file}/models/best_model_with_{n_runs - 1}_graphs_{net_}.pt"
        model, bc_pos, bc_dpos = choose_training_model(config, device)
        state_dict = torch.load(net, map_location=device)
        model.load_state_dict(state_dict['model_state_dict'])
        model.edges = edge_index
        print(f'net: {net}')
        embedding = get_embedding(model.a, 1)

        fig, ax = fig_init()
        for n in range(n_particle_types):
            c_ = np.round(n / (n_particle_types - 1) * 256).astype(int)
            plt.scatter(embedding[index_particles[n], 0], embedding[index_particles[n], 1], s=400,
                        alpha=0.1)  # , color=cmap.color(c_)
        # plt.xlabel(r'$\ensuremath{\mathbf{a}}_{i0}$', fontsize=64)
        # plt.ylabel(r'$\ensuremath{\mathbf{a}}_{i1}$', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/embedding_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        k = 500
        x = x_list[1][k].clone().detach()
        dataset = data.Data(x=x[:, :], edge_index=model.edges)
        y = y_list[1][k].clone().detach()
        y = y
        pred = model(dataset, data_id=1)
        adj_t = adjacency > 0
        edge_index = adj_t.nonzero().t().contiguous()
        edge_attr_adjacency = adjacency[adj_t]
        dataset = data.Data(x=x, pos=x[:, 1:3], edge_index=edge_index, edge_attr=edge_attr_adjacency)

        fig, ax = fig_init()
        gt_weight = to_numpy(adjacency[adj_t])
        pred_weight = to_numpy(model.weight_ij[adj_t])
        plt.scatter(gt_weight, pred_weight, s=200, c='k')
        x_data = gt_weight
        y_data = pred_weight.squeeze()
        lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)
        residuals = y_data - linear_model(x_data, *lin_fit)
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
        r_squared = 1 - (ss_res / ss_tot)
        plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
        plt.ylabel('Reconstructed $A_{ij}$ values', fontsize=64)
        plt.xlabel('True network $A_{ij}$ values', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/Matrix_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        print(f"R^2$: {np.round(r_squared, 3)}  Slope: {np.round(lin_fit[0], 2)}   offset: {np.round(lin_fit[1], 2)}  ")

        fig, ax = fig_init()
        uu = x[:, 6:7].squeeze()
        ax.xaxis.get_major_formatter()._usetex = False
        ax.yaxis.get_major_formatter()._usetex = False
        uu = torch.tensor(np.linspace(0, 3, 1000)).to(device)
        print(n_particles)
        func_list, proj_interaction = analyze_edge_function(rr=uu, vizualize=True, config=config,
                                                            model_lin_edge=model.lin_phi, model_a=model.a,
                                                            dataset_number=1,
                                                            n_particles=int(
                                                                n_particles * (1 - config.training.particle_dropout)),
                                                            ynorm=ynorm,
                                                            types=to_numpy(x[:, 5]),
                                                            cmap=cmap, device=device)
        # plt.xlabel(r'$d_{ij}$', fontsize=64)
        # plt.ylabel(r'$f(\ensuremath{\mathbf{a}}_i, d_{ij})$', fontsize=64)
        plt.xlabel(r'$u$', fontsize=64)
        plt.ylabel(r'Reconstructed $\Phi(u)$', fontsize=64)
        plt.ylim([-0.25, 0.25])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/phi_u_{config_file}_{net_}.tif", dpi=170.7)
        plt.close()

        embedding_ = model.a[1, :, :]
        u = torch.tensor(0.5, device=device).float()
        u = u * torch.ones((n_particles, 1), device=device)
        in_features = torch.cat((u, embedding_), dim=1)
        with torch.no_grad():
            func = model.lin_phi(in_features.float())
        func = func[:, 0]
        proj_interaction = to_numpy(func[:, None])
        labels, n_clusters = embedding_cluster.get(proj_interaction, 'kmeans_auto')

        fig, ax = fig_init()
        for n in range(n_clusters):
            pos = np.argwhere(labels == n)
            pos = np.array(pos)
            if pos.size > 0:
                plt.scatter(np.ones_like(pos) * 0.5, proj_interaction[pos, 0], color=cmap.color(n), s=400, alpha=0.1)
        label_list = []
        for n in range(n_particle_types):
            tmp = labels[index_particles[n]]
            label_list.append(np.round(np.median(tmp)))
        label_list = np.array(label_list)
        plt.xlabel(r'$u$', fontsize=64)
        plt.ylabel(r'$\Phi(u)$', fontsize=64)
        plt.ylim([-0.25, 0.25])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/cluster_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        new_labels = labels.copy()
        for n in range(n_particle_types):
            new_labels[labels == label_list[n]] = n
        type_list = x[:, 5:6].clone().detach()
        accuracy = metrics.accuracy_score(to_numpy(type_list), new_labels)
        print(f'accuracy: {np.round(accuracy, 2)}   n_clusters: {n_clusters}')

        model_a_ = model.a[1].clone().detach()
        for n in range(n_clusters):
            pos = np.argwhere(labels == n).squeeze().astype(int)
            pos = np.array(pos)
            if pos.size > 0:
                median_center = model_a_[pos, :]
                median_center = torch.median(median_center, dim=0).values
                model_a_[pos, :] = median_center
        with torch.no_grad():
            model.a[1] = model_a_.clone().detach()

        fig, ax = fig_init()
        uu = torch.tensor(np.linspace(0, 3, 1000)).to(device)
        p = config.simulation.params
        if len(p) > 1:
            p = torch.tensor(p, device=device)
        fig_ = plt.figure(figsize=(12, 12))
        for n in range(n_particle_types):
            phi = -p[n, 0] * uu + p[n, 1] * torch.tanh(uu)
            plt.plot(to_numpy(uu), to_numpy(phi), linewidth=8)
        plt.xlabel(r'$u$', fontsize=64)
        plt.ylabel(r'True $\Phi(u)$', fontsize=64)
        plt.ylim([-0.25, 0.25])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_phi_u_{config_file}_{net_}.tif", dpi=170.7)
        plt.close()

        uu = torch.tensor(np.linspace(0, 3, 1000)).to(device)
        with torch.no_grad():
            func = model.lin_edge(uu[:, None].float())
        true_func = torch.tanh(uu[:, None].float())

        fig, ax = fig_init()
        plt.xlabel(r'$u$', fontsize=64)
        plt.ylabel(r'Reconstructed $f(u)$', fontsize=64)
        plt.scatter(to_numpy(uu), to_numpy(func), linewidth=8, c='k', label='Reconstructed')
        plt.ylim([-3, 3])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/f_u_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        fig, ax = fig_init()
        plt.xlabel(r'$u$', fontsize=64)
        plt.ylabel(r'True $f(u)$', fontsize=64)
        plt.scatter(to_numpy(uu), to_numpy(true_func), linewidth=8, c='k', label='Reconstructed')
        plt.ylim([-3, 3])
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/true_f_u_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        bFit = False

        if bFit:
            uu = torch.tensor(np.linspace(0, 3, 1000)).to(device)
            in_features = uu[:, None]
            with torch.no_grad():
                func = model.lin_edge(in_features.float())
                func = func[:, 0]

            uu = uu.to(dtype=torch.float32)
            func = func.to(dtype=torch.float32)
            dataset = {}
            dataset['train_input'] = uu[:, None]
            dataset['test_input'] = uu[:, None]
            dataset['train_label'] = func[:, None]
            dataset['test_label'] = func[:, None]

            model_pysrr = PySRRegressor(
                niterations=30,  # < Increase me for better results
                binary_operators=["+", "*"],
                unary_operators=[
                    "cos",
                    "exp",
                    "sin",
                    "tanh"
                ],
                random_state=0,
                temp_equation_file=True
            )

            model_pysrr.fit(to_numpy(dataset["train_input"]), to_numpy(dataset["train_label"]))

            print(model_pysrr)
            print(model_pysrr.equations_)

            k = 500
            x = x_list[1][k].clone().detach()
            dataset = data.Data(x=x[:, :], edge_index=model.edges)
            y = y_list[1][k].clone().detach()
            y = y
            pred = model(dataset, data_id=1)
            adj_t = adjacency > 0
            edge_index = adj_t.nonzero().t().contiguous()
            edge_attr_adjacency = adjacency[adj_t]
            dataset = data.Data(x=x, pos=x[:, 1:3], edge_index=edge_index, edge_attr=edge_attr_adjacency)

            fig, ax = fig_init()
            gt_weight = to_numpy(adjacency[adj_t])
            pred_weight = to_numpy(model.weight_ij[adj_t]) * -1.878
            plt.scatter(gt_weight, pred_weight, s=200, c='k')
            x_data = gt_weight
            y_data = pred_weight.squeeze()
            lin_fit, lin_fitv = curve_fit(linear_model, x_data, y_data)
            residuals = y_data - linear_model(x_data, *lin_fit)
            ss_res = np.sum(residuals ** 2)
            ss_tot = np.sum((y_data - np.mean(y_data)) ** 2)
            r_squared = 1 - (ss_res / ss_tot)
            plt.plot(x_data, linear_model(x_data, lin_fit[0], lin_fit[1]), color='r', linewidth=4)
            plt.ylabel('Reconstructed $A_{ij}$ values', fontsize=64)
            plt.xlabel('True network $A_{ij}$ values', fontsize=64)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/Matrix_bis_{config_file}_{net_}.tif", dpi=300)
            plt.close()

            print(
                f"R^2$: {np.round(r_squared, 3)}  Slope: {np.round(lin_fit[0], 2)}   offset: {np.round(lin_fit[1], 2)}  ")

            fig, ax = fig_init()
            plt.xlabel(r'$u$', fontsize=64)
            plt.ylabel(r'Reconstructed $f(u)$', fontsize=64)
            plt.plot(to_numpy(uu), to_numpy(true_func), linewidth=20, c='g', label='True')
            plt.plot(to_numpy(uu), to_numpy(func) / -1.878, linewidth=8, c='k', label='Reconstructed')
            plt.legend(fontsize=32.0)
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/comparison_f_u_{config_file}_{net_}.tif", dpi=300)
            plt.close()

            uu = torch.tensor(np.linspace(0, 3, 1000)).to(device)
            fig, ax = fig_init()
            n = 0
            pos = np.argwhere(labels == n).squeeze().astype(int)
            func = torch.mean(func_list[pos, :], dim=0)
            true_func = -to_numpy(uu) * to_numpy(p[n, 0]) + to_numpy(p[n, 1]) * np.tanh(to_numpy(uu))
            plt.plot(to_numpy(uu), true_func, linewidth=20, label='True', c='orange')  # xkcd:sky blue') #'orange') #
            plt.plot(to_numpy(uu), to_numpy(func), linewidth=8, c='k', label='Reconstructed')
            plt.xlabel(r'$u$', fontsize=64)
            plt.ylabel(r'Reconstructed $\Phi_1(u)$', fontsize=64)
            plt.legend(fontsize=32.0)
            plt.ylim([-0.25, 0.25])
            plt.tight_layout()
            plt.savefig(f"./{log_dir}/results/comparison_phi_1_{config_file}_{net_}.tif", dpi=300)
            plt.close()

            uu = uu.to(dtype=torch.float32)
            func = func.to(dtype=torch.float32)
            dataset = {}
            dataset['train_input'] = uu[:, None]
            dataset['test_input'] = uu[:, None]
            dataset['train_label'] = func[:, None]
            dataset['test_label'] = func[:, None]

            model_pysrr = PySRRegressor(
                niterations=300,  # < Increase me for better results
                binary_operators=["+", "*"],
                unary_operators=[
                    "tanh"
                ],
                random_state=0,
                temp_equation_file=True
            )
            model_pysrr.fit(to_numpy(dataset["train_input"]), to_numpy(dataset["train_label"]))

            print(model_pysrr)
            print(model_pysrr.equations_)

            # for col in model_pysrr.equations_.columns:
            #     print(col)

        k = 500
        x = x_list[1][k].clone().detach()
        dataset = data.Data(x=x[:, :], edge_index=model.edges)
        y = y_list[1][k].clone().detach()
        y = y
        pred, msg, phi, input_phi = model(dataset, data_id=1, return_all=True)
        u_j = model.u_j
        activation = model.activation
        adj_t = adjacency > 0
        edge_index = adj_t.nonzero().t().contiguous()
        edge_attr_adjacency = adjacency[adj_t]
        dataset = data.Data(x=x, pos=x[:, 1:3], edge_index=edge_index, edge_attr=edge_attr_adjacency)
        du_gt, msg_gt, phi_gt = GT_model(dataset, return_all=True)
        u_j_gt = GT_model.u_j
        activation_gt = GT_model.activation
        uu = x[:, 6:7].squeeze()

        fig, ax = fig_init()
        plt.scatter(to_numpy(uu), to_numpy(msg + phi), s=100)
        plt.scatter(to_numpy(uu), to_numpy(phi), s=20)
        plt.scatter(to_numpy(uu), to_numpy(msg), s=20)
        # plt.scatter(to_numpy(uu), to_numpy(msg_gt+phi_gt), s=40, c='r')
        plt.xlim([0, 3])
        plt.ylim([0, 1])
        plt.savefig(f"./{log_dir}/results/model_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        fig, ax = fig_init()
        plt.scatter(to_numpy(uu), to_numpy(msg_gt + phi_gt), s=100)
        plt.scatter(to_numpy(uu), to_numpy(phi_gt), s=20)
        plt.scatter(to_numpy(uu), to_numpy(msg_gt), s=20)
        plt.xlim([0, 3])
        plt.ylim([0, 1])
        plt.savefig(f"./{log_dir}/results/true_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        fig, ax = fig_init()
        plt.scatter(to_numpy(uu), to_numpy(msg + phi), s=100)
        plt.scatter(to_numpy(uu), to_numpy(msg_gt + phi_gt), s=20)
        plt.savefig(f"./{log_dir}/results/comparison_all_{config_file}_{net_}.tif", dpi=300)
        fig, ax = fig_init()

        fig, ax = fig_init()
        plt.scatter(to_numpy(msg_gt), to_numpy(msg), s=20, c='k')
        plt.savefig(f"./{log_dir}/results/comparison_msg_{config_file}_{net_}.tif", dpi=300)
        fig, ax = fig_init()

        fig, ax = fig_init()
        plt.scatter(to_numpy(u_j_gt), to_numpy(activation_gt), s=20)
        plt.scatter(to_numpy(u_j), to_numpy(activation), s=20)
        plt.scatter(to_numpy(uu), to_numpy(phi_gt), s=20)
        plt.scatter(to_numpy(uu), to_numpy(phi), s=20)
        plt.savefig(f"./{log_dir}/results/funky_comparison_{config_file}_{net_}.tif", dpi=300)
        fig, ax = fig_init()

        fig, ax = fig_init()
        plt.scatter(to_numpy(uu), to_numpy(phi), s=400, c='g', label='True')
        plt.scatter(to_numpy(uu), to_numpy(phi_gt), s=20, c='k', label='Reconstructed')
        plt.xlabel(r'$u$', fontsize=64)
        plt.ylabel(r'$f(u)$', fontsize=64)
        plt.legend(fontsize=32.0)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/phi_u_{config_file}_{net_}.tif", dpi=300)
        plt.close()

        # model_kan = KAN(width=[1, 1], grid=5, k=3, seed=0)
        # model_kan.train(dataset, opt="LBFGS", steps=20, lamb=0.01, lamb_entropy=10.)
        # lib = ['x', 'x^2', 'x^3', 'x^4', 'exp', 'log', 'sqrt', 'tanh', 'sin', 'abs']
        # model_kan.auto_symbolic(lib=lib)
        # model_kan.train(dataset, steps=20)
        # formula, variables = model_kan.symbolic_formula()
        # print(formula)
        #
        # model_kan = KAN(width=[1, 5, 1], grid=5, k=3, seed=0)
        # model_kan.train(dataset, opt="LBFGS", steps=50, lamb=0.01, lamb_entropy=10.)
        # model_kan = model_kan.prune()
        # model_kan.train(dataset, opt="LBFGS", steps=50);
        # for k in range(10):
        #     lib = ['x', 'x^2', 'x^3', 'x^4', 'exp', 'log', 'sqrt', 'tanh', 'sin', 'abs']
        #     model_kan.auto_symbolic(lib=lib)
        #     model_kan.train(dataset, steps=100)
        #     formula, variables = model_kan.symbolic_formula()
        #     print(formula)


def data_video_validation(config_file, epoch_list, log_dir, logger, device):
    print('')

    # Load parameters from config file
    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    print(f'Save movie ... {config.graph_model.particle_model_name} {config.graph_model.mesh_model_name}')

    graph_files = glob.glob(f"./graphs_data/graphs_{dataset_name}/generated_data/*")
    N_files = len(graph_files)
    recons_files = glob.glob(f"{log_dir}/tmp_recons/*")

    # import cv2
    # fourcc = cv2.VideoWriter_fourcc(*'XVID')
    # out = cv2.VideoWriter(f"video/validation_{dataset_name}.avi", fourcc, 20.0, (1024, 2048))

    os.makedirs(f"video_tmp/{config_file}", exist_ok=True)

    for n in trange(N_files):
        generated = imread(graph_files[n])
        reconstructed = imread(recons_files[n])
        frame = np.concatenate((generated[:, :, 0:3], reconstructed[:, :, 0:3]), axis=1)
        # out.write(frame)
        imsave(f"video_tmp/{config_file}/{dataset_name}_{10000 + n}.tif", frame)

    # Release the video writer
    # out.release()

    # print("Video saved as 'output.avi'")


def data_video_training(config_file, epoch_list, log_dir, logger, device):
    print('')

    # Load parameters from config file
    config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
    dataset_name = config.dataset

    max_radius = config.simulation.max_radius
    if config.graph_model.particle_model_name != '':
        config_model = config.graph_model.particle_model_name
    elif config.graph_model.signal_model_name != '':
        config_model = config.graph_model.signal_model_name
    elif config.graph_model.mesh_model_name != '':
        config_model = config.graph_model.mesh_model_name

    print(f'Save movie ... {config.graph_model.particle_model_name} {config.graph_model.mesh_model_name}')

    embedding = imread(f"{log_dir}/embedding.tif")
    function = imread(f"{log_dir}/function.tif")
    # field = imread(f"{log_dir}/field.tif")

    matplotlib.use("Qt5Agg")

    os.makedirs(f"video_tmp/{config_file}_training", exist_ok=True)

    for n in trange(embedding.shape[0]):
        fig = plt.figure(figsize=(16, 8))
        ax = fig.add_subplot(1, 2, 1)
        ax.imshow(embedding[n, :, :, 0:3])
        plt.xlabel(r'$a_{i0}$', fontsize=32)
        plt.ylabel(r'$a_{i1}$', fontsize=32)
        plt.xticks([])
        plt.yticks([])
        match config_file:
            case 'wave_slit':
                if n < 50:
                    plt.text(0, 1.1, f'epoch = 0,   it = {n * 200}', ha='left', va='top', transform=ax.transAxes,
                             fontsize=32)
                else:
                    plt.text(0, 1.1, f'Epoch={n - 49}', ha='left', va='top', transform=ax.transAxes, fontsize=32)
            case 'arbitrary_3':
                if n < 17:
                    plt.text(0, 1.1, f'epoch = 0,   it = {n * 200}', ha='left', va='top', transform=ax.transAxes,
                             fontsize=32)
                else:
                    plt.text(0, 1.1, f'epoch = {n - 16}', ha='left', va='top', transform=ax.transAxes, fontsize=32)
            case 'arbitrary_3_field_video_bison_siren_with_time':
                if n < 13 * 3:
                    plt.text(0, 1.1, f'epoch= {n // 13} ,   it = {(n % 13) * 500}', ha='left', va='top',
                             transform=ax.transAxes, fontsize=32)
                else:
                    plt.text(0, 1.1, f'epoch = {n - 13 * 3 + 3}', ha='left', va='top', transform=ax.transAxes,
                             fontsize=32)
            case 'arbitrary_64_256':
                if n < 51:
                    plt.text(0, 1.1, f'epoch= 0 ,   it = {n * 200}', ha='left', va='top', transform=ax.transAxes,
                             fontsize=32)
                else:
                    plt.text(0, 1.1, f'epoch = {n - 50}', ha='left', va='top', transform=ax.transAxes, fontsize=32)
            case 'boids_16_256' | 'gravity_16':
                if n < 50:
                    plt.text(0, 1.1, f'epoch = 0,   it = {n * 200}', ha='left', va='top', transform=ax.transAxes,
                             fontsize=32)
                else:
                    plt.text(0, 1.1, f'Epoch={n - 49}', ha='left', va='top', transform=ax.transAxes, fontsize=32)

        ax = fig.add_subplot(1, 2, 2)
        ax.imshow(function[n, :, :, 0:3])
        # plt.ylabel(r'$f(a_i,d_{ij})$', fontsize=32)
        # plt.xlabel(r'$d_{ij}$', fontsize=32)
        plt.ylabel('x', fontsize=32)
        plt.xlabel('y', fontsize=32)
        plt.xticks(fontsize=16.0)
        plt.yticks(fontsize=16.0)
        ax.xaxis.set_major_locator(plt.MaxNLocator(5))
        ax.yaxis.set_major_locator(plt.MaxNLocator(5))
        # fmt = lambda x, pos: '{:.3f}'.format(x / 1000 * max_radius, pos)
        # ax.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))

        match config_file:
            case 'wave_slit':
                fmt = lambda x, pos: '{:.1f}'.format((x / 1000), pos)
                ax.xaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
                fmt = lambda x, pos: '{:.1f}'.format((1 - x / 1000), pos)
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            case 'arbitrary_3_field_video_bison_siren_with_time':
                fmt = lambda x, pos: '{:.2f}'.format(-x / 1000 * 0.7 + 0.3, pos)
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            case 'arbitrary_3':
                fmt = lambda x, pos: '{:.2f}'.format(-x / 1000 * 0.7 + 0.3, pos)
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            case 'arbitrary_64_256':
                fmt = lambda x, pos: '{:.2f}'.format(-x / 1000 * 0.7 + 0.3, pos)
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            case 'boids_16_256':
                fmt = lambda x, pos: '{:.2f}e-4'.format((-x / 1000 + 0.5) * 2, pos)
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))
            case 'boids_16_256' | 'gravity_16':
                fmt = lambda x, pos: '{:.1f}e5'.format((1 - x / 1000) * 5, pos)
                ax.yaxis.set_major_formatter(mpl.ticker.FuncFormatter(fmt))

        # ax = fig.add_subplot(1, 3, 3)
        # ax.imshow(field[n, :, :, 0:3],cmap='grey')

        plt.tight_layout()
        plt.tight_layout()
        plt.savefig(f"video_tmp/{config_file}_training/training_{config_file}_{10000 + n}.tif", dpi=64)
        plt.close()

    # plt.text(0, 1.05, f'Frame {it}', ha='left', va='top', transform=ax.transAxes, fontsize=32)
    # ax.tick_params(axis='both', which='major', pad=15)


def data_plot(config_file, epoch_list, device):
    plt.rcParams['text.usetex'] = True
    rc('font', **{'family': 'serif', 'serif': ['Palatino']})
    matplotlib.rcParams['savefig.pad_inches'] = 0
    matplotlib.use("Qt5Agg")

    l_dir = os.path.join('.', 'log')
    log_dir = os.path.join(l_dir, 'try_{}'.format(config_file))
    print('log_dir: {}'.format(log_dir))


    logging.basicConfig(filename=f'{log_dir}/results.log', format='%(asctime)s %(message)s', filemode='w')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if config.training.sparsity != 'none':
        print(
            f'GNN trained with simulation {config.graph_model.particle_model_name} ({config.simulation.n_particle_types} types), with cluster method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
        logger.info(
            f'GNN trained with simulation {config.graph_model.particle_model_name} ({config.simulation.n_particle_types} types), with cluster method: {config.training.cluster_method}   threshold: {config.training.cluster_distance_threshold}')
    else:
        print(
            f'GNN trained with simulation {config.graph_model.particle_model_name} ({config.simulation.n_particle_types} types), no clustering')
        logger.info(
            f'GNN trained with simulation {config.graph_model.particle_model_name} ({config.simulation.n_particle_types} types), no clustering')

    if os.path.exists(f'{log_dir}/loss.pt'):
        loss = torch.load(f'{log_dir}/loss.pt')
        fig, ax = fig_init(formatx='%.0f', formaty='%.5f')
        plt.plot(loss, color='k', linewidth=4)
        plt.xlim([0, 20])
        plt.ylabel('Loss', fontsize=64)
        plt.xlabel('Epochs', fontsize=64)
        plt.tight_layout()
        plt.savefig(f"./{log_dir}/results/loss_{config_file}.tif", dpi=170.7)
        plt.close()
        print('final loss {:.3e}'.format(loss[-1]))
        logger.info('final loss {:.3e}'.format(loss[-1]))

    match config.graph_model.particle_model_name:
        case 'PDE_A':
            if config.simulation.non_discrete_level>0:
                data_plot_attraction_repulsion_continuous(config_file, epoch_list, log_dir, logger, device)
            elif config.training.has_no_tracking:
                data_plot_attraction_repulsion_tracking(config_file, epoch_list, log_dir, logger, device)
            else:
                data_plot_attraction_repulsion(config_file, epoch_list, log_dir, logger, device)
        case 'PDE_A_bis':
            data_plot_attraction_repulsion_asym(config_file, epoch_list, log_dir, logger, device)
        case 'PDE_B':
            data_plot_boids(config_file, epoch_list, log_dir, logger, device)
        case 'PDE_ParticleField_B' | 'PDE_ParticleField_A':
            data_plot_particle_field(config_file, 'grey', device)
        case 'PDE_E':
            data_plot_Coulomb(config_file, epoch_list, log_dir, logger, device)
        case 'PDE_G':
            if config_file == 'gravity_100':
                data_plot_gravity_continuous(config_file, epoch_list, log_dir, logger, device)
            else:
                data_plot_gravity(config_file, epoch_list, log_dir, logger, device)

    match config.graph_model.mesh_model_name:
        case 'WaveMesh':
            data_plot_wave(config_file=config_file, epoch_list=epoch_list, log_dir=log_dir, logger=logger, cc='viridis',
                           device=device)

    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    print(' ')
    print(' ')


if __name__ == '__main__':

    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    print(' ')
    print(f'device {device}')
    print(' ')
    
    # config_list = ['boids_16_256_bison_siren_with_time_2']
    # config_list = ['boids_16_256','boids_32_256','boids_64_256']
    # config_list = ['boids_16_256_division_death_model_2']
    # config_list = ['Coulomb_3_256']
    # config_list = ['wave_slit_test']
    # config_list = ['Coulomb_3_256']
    # config_list = ['arbitrary_64', 'arbitrary_64_0_1', 'arbitrary_64_0_01', 'arbitrary_64_0_005'] #, 'arbitrary_3', 'arbitrary_16', 'arbitrary_32', 'arbitrary_16_noise_0_1', 'arbitrary_16_noise_0_2', 'arbitrary_16_noise_0_3', 'arbitrary_16_noise_0_4', 'arbitrary_16_noise_0_5']
    # config_list = ['arbitrary_3_continuous']
    # config_list = ['arbitrary_64_0_01']
    # config_list = ['boids_16_256','boids_32_256','boids_64_256']
    config_list = ['arbitrary_3_tracking']

    epoch_list = ['0_500','0_1000','0_2000','0_5000','0_10000','0_20000','0_49000'] #,'0','1_500','1_1000','1_2000','1_5000','1_10000','1_20000','1_49000','1','2_500','2_1000','2_2000','2_5000','2_10000','2_20000','2_49000','2','3','4','5','6','7','8','9','10','11','12','13','14','15','16','17','18','19','20']

    for config_file in config_list:
        config = ParticleGraphConfig.from_yaml(f'./config/{config_file}.yaml')
        data_plot(config_file, epoch_list, device)

        # data_video_validation(config_file,device=device)
        # data_video_training(config_file,device=device)
