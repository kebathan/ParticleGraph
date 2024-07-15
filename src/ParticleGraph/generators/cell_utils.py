import numpy as np
from scipy.spatial import Voronoi, voronoi_plot_2d
import torch
from ParticleGraph.utils import to_numpy

def init_cell_range(config, device, scenario="None"):
    simulation_config = config.simulation
    n_particles = simulation_config.n_particles
    n_particle_types = simulation_config.n_particle_types

    ##### defines all variables for the cell model, per type of cell: dimension = n_particle_types

    if config.simulation.cell_cycle_length != [-1]:
        cycle_length = torch.tensor(config.simulation.cell_cycle_length, device=device)
    else:
        cycle_length = torch.clamp(torch.abs(
            torch.ones(n_particle_types, 1, device=device) * 250 + torch.randn(n_particle_types, 1,
                                                                               device=device) * 50), min=100,
                                   max=700).squeeze()

    if config.simulation.final_cell_mass != [-1]:
        final_cell_mass = torch.tensor(config.simulation.final_cell_mass, device=device)
    else:
        final_cell_mass = torch.clamp(torch.abs(
            torch.ones(n_particle_types, 1, device=device) * 250 + torch.randn(n_particle_types, 1,
                                                                               device=device) * 25), min=200,
                                      max=500).flatten()

    if config.simulation.cell_death_rate != [-1]:
        cell_death_rate = torch.tensor(config.simulation.cell_death_rate, device=device)
    else:
        cell_death_rate = torch.zeros((n_particles, 1), device=device)

    if config.simulation.mc_slope != [-1]:
        mc_slope = torch.tensor(config.simulation.mc_slope, device=device)
    else:
        mc_slope = torch.clamp(torch.randn(n_particle_types, 1, device=device) * 30, min=-30, max=30).flatten()

    return cycle_length, final_cell_mass, cell_death_rate, mc_slope


def init_cells(config, cycle_length, final_cell_mass, cell_death_rate, mc_slope, device):
    simulation_config = config.simulation
    n_particles = simulation_config.n_particles
    n_particle_types = simulation_config.n_particle_types
    dimension = simulation_config.dimension

    dpos_init = simulation_config.dpos_init

    if (simulation_config.boundary == 'periodic'):  # | (simulation_config.dimension == 3):
        pos = torch.rand(n_particles, dimension, device=device)
    else:
        pos = torch.randn(n_particles, dimension, device=device) * 0.5

    ###### specify all variables per cell, dimension = n_particles

    # specify position
    dpos = dpos_init * torch.randn((n_particles, dimension), device=device)
    dpos = torch.clamp(dpos, min=-torch.std(dpos), max=+torch.std(dpos))
    # specify type
    type = torch.zeros(int(n_particles / n_particle_types), device=device)
    for n in range(1, n_particle_types):
        type = torch.cat((type, n * torch.ones(int(n_particles / n_particle_types), device=device)), 0)
    if (simulation_config.params == 'continuous') | (
            config.simulation.non_discrete_level > 0):  # TODO: params is a list[list[float]]; this can never happen?
        type = torch.tensor(np.arange(n_particles), device=device)
    # specify cell status dim=2  H1[:,0] = cell alive flag, alive : 0 , death : 0 , H1[:,1] = cell division flag, dividing : 1
    status = torch.ones(n_particles, 2, device=device)
    status[:, 1] = 0

    cycle_length_distrib = cycle_length[to_numpy(type)] * (
                torch.ones(n_particles, device=device) + 0.05 * torch.randn(n_particles, device=device))
    cycle_length_distrib = cycle_length_distrib[:, None]

    mc_slope_distrib = mc_slope[to_numpy(
        type), None]  # * (torch.ones(n_particles, device=device) + 0.05 * torch.randn(n_particles, device=device))

    cell_age = torch.rand(n_particles, device=device)
    cell_age = cell_age * cycle_length[to_numpy(type)].squeeze()
    cell_age = cell_age[:, None]

    cell_stage = update_cell_cycle_stage(n_particles, cell_age, cycle_length, type, device)

    growth_rate = final_cell_mass / (2 * cycle_length)
    growth_rate_distrib = growth_rate[to_numpy(type)].squeeze()[:, None]

    cell_mass_distrib = (growth_rate_distrib * cell_age) + (final_cell_mass[to_numpy(type), None] / 2)

    cell_death_rate_distrib = (cell_death_rate[to_numpy(type)].squeeze() * (
                torch.ones(n_particles, device=device) + 0.05 * torch.randn(n_particles, device=device))) / 100
    cell_death_rate_distrib = cell_death_rate_distrib[:, None]

    particle_id = torch.arange(n_particles, device=device)
    particle_id = particle_id[:, None]
    type = type[:, None]

    return particle_id, pos, dpos, type, status, cell_age, cell_stage, cell_mass_distrib, growth_rate_distrib, cycle_length_distrib, cell_death_rate_distrib, mc_slope_distrib


def update_cell_cycle_stage(n_particles, cell_age, cycle_length, type_list, device):
    g1 = 0.46
    s = 0.33
    g2 = 0.17
    m = 0.04

    G1 = (g1 * cycle_length).squeeze()
    S = ((g1 + s) * cycle_length).squeeze()
    G2 = ((g1 + s + g2) * cycle_length).squeeze()
    M = ((g1 + s + g2 + m) * cycle_length).squeeze()

    cell_age = cell_age.squeeze()

    cell_stage = torch.zeros(n_particles, device=device)
    for i in range(n_particles):
        curr = cell_age[i]

        if curr <= G1[int(type_list[i])]:
            cell_stage[i] = 0
        elif curr <= S[int(type_list[i])]:
            cell_stage[i] = 1
        elif curr <= G2[int(type_list[i])]:
            cell_stage[i] = 2
        else:
            cell_stage[i] = 3

    return cell_stage[:, None]


def get_vertices(points=[], device=[]):

    extra_points = points
    v_list = [[-1, -1], [-1, 0], [-1, 1], [0, 1], [1, 1], [1, 0], [1, -1], [0, -1]]
    for n in range(len(v_list)):
        extra_points = np.concatenate((extra_points, points + v_list[n]), axis=0)
    vor = Voronoi(extra_points)

    # vertices_index collect all vertices index of regions of interest
    vertices_per_cell = []
    for n in range(len(points)):
        if n == 0:
            vertices_index = vor.regions[vor.point_region[0]]
        else:
            vertices_index = np.concatenate((vertices_index, vor.regions[vor.point_region[n]]), axis=0)
        vertices_per_cell.append(vor.regions[vor.point_region[n]])
    vertices = []
    map = {}
    count = 0
    for i in range(len(vertices_per_cell)):
        for j in range(len(vertices_per_cell[i])):
            if vertices_per_cell[i][j] in map:
                vertices_per_cell[i][j] = map[vertices_per_cell[i][j]]
            else:
                map[vertices_per_cell[i][j]] = count
                vertices.append(vor.vertices[vertices_per_cell[i][j]])
                vertices_per_cell[i][j] = map[vertices_per_cell[i][j]]
                count += 1
    vertices_pos = np.array(vertices)
    vertices_pos = torch.tensor(vertices_pos, device=device)

    return vor, vertices_pos, vertices_per_cell

def get_voronoi_area(x_pos, vertices_pos, vertices_per_cell, device):
    area = []
    return area

def get_voronoi_perimeter(vertices_pos, vertices_per_cell, device):
    perimeter = []
    return perimeter

def get_voronoi_lengths(vertices_pos, vertices_per_cell, device):
    lengths = []
    return lengths


def cell_energy(voronoi_area, voronoi_perimeter, voronoi_lengths, device):

    energy = []
    return energy












# fig, ax = fig_init()
# voronoi_plot_2d(vor, ax=ax, show_vertices=False, line_colors='black', line_width=1, line_alpha=0.5,
#                 point_size=0)
# plt.scatter(points[:, 0], points[:, 1], s=30, color='blue')
# plt.scatter(vertices[:, 0], vertices[:, 1], s=30, color='green')
# plt.xlim([-0.1, 1.1])
# plt.ylim([-0.1, 1.1])