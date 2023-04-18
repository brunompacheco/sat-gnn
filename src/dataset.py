from copy import deepcopy
from pathlib import Path
import pickle
import json

import dgl
import gurobipy
import numpy as np
import torch
from dgl.data import DGLDataset

from src.problem import get_model


def make_graph_from_matrix(A, b, c):
    # create graph
    n_var = c.shape[0]
    n_con = b.shape[0]

    edges = np.indices(A.shape)  # cons -> vars

    # get only real (non-null) edges
    A_ = A.flatten()
    edges = edges.reshape(edges.shape[0],-1)
    edges = edges[:,A_ != 0]
    edges = torch.from_numpy(edges)

    edge_weights = A_[A_ != 0]

    g = dgl.heterograph({('var', 'v2c', 'con'): (edges[1], edges[0]),
                            ('con', 'c2v', 'var'): (edges[0], edges[1]),},
                            num_nodes_dict={'var': n_var, 'con': n_con,})

    g.edges['v2c'].data['A'] = torch.from_numpy(edge_weights)
    g.edges['c2v'].data['A'] = torch.from_numpy(edge_weights)

    g.nodes['var'].data['x'] = torch.from_numpy(c)
    g.nodes['con'].data['x'] = torch.from_numpy(b)

    return g

def make_graph_from_model(model):
    # TODO: include variable bounds (not present in getA())
    A = model.getA().toarray()
    # TODO: include sos variable constraints
    b = np.array(model.getAttr('rhs'))
    c = np.array(model.getAttr('obj'))

    # get only real (non-null) edges
    A_ = A.flatten()
    edges = np.indices(A.shape)  # cons -> vars
    edges = edges.reshape(edges.shape[0],-1)
    edges = edges[:,A_ != 0]
    # edges = torch.from_numpy(edges)

    edge_weights = A_[A_ != 0]

    constraints_sense = np.array([ci.sense for ci in model.getConstrs()])
    constraints_sense = np.array(list(map({'>': 1, '=': 0, '<': -1}.__getitem__, constraints_sense)))

    vars_names = [v.getAttr(gurobipy.GRB.Attr.VarName) for v in model.getVars()]
    # grab all non-decision variables (everything that is not `x` or `phi`)
    soc_vars_mask = np.array([('x(' not in v) and ('phi(' not in v) for v in vars_names])
    soc_vars = np.arange(soc_vars_mask.shape[0])[soc_vars_mask]
    var_vars = np.arange(soc_vars_mask.shape[0])[~soc_vars_mask]
    soc_edges_mask = np.isin(edges.T[:,1], soc_vars)

    var_edges = edges[:,~soc_edges_mask]
    soc_edges = edges[:,soc_edges_mask]

    # translate soc/var nodes index to 0-based
    soc_edges[1] = np.array(list(map(
        dict(zip(soc_vars, np.arange(soc_vars.shape[0]))).get,
        soc_edges[1]
    )))
    var_edges[1] = np.array(list(map(
        dict(zip(var_vars, np.arange(var_vars.shape[0]))).get,
        var_edges[1]
    )))

    g = dgl.heterograph({
        ('var', 'v2c', 'con'): (var_edges[1], var_edges[0]),
        ('con', 'c2v', 'var'): (var_edges[0], var_edges[1]),
        ('soc', 's2c', 'con'): (soc_edges[1], soc_edges[0]),
        ('con', 'c2s', 'soc'): (soc_edges[0], soc_edges[1]),
    })

    soc_edge_weights = edge_weights[soc_edges_mask]
    g.edges['s2c'].data['A'] = torch.from_numpy(soc_edge_weights)
    g.edges['c2s'].data['A'] = torch.from_numpy(soc_edge_weights)

    var_edge_weights = edge_weights[~soc_edges_mask]
    g.edges['v2c'].data['A'] = torch.from_numpy(var_edge_weights)
    g.edges['c2v'].data['A'] = torch.from_numpy(var_edge_weights)

    g.nodes['con'].data['x'] = torch.from_numpy(np.stack(
        (b, constraints_sense), -1
    ))

    g.nodes['var'].data['x'] = torch.from_numpy(c[~soc_vars_mask])
    g.nodes['soc'].data['x'] = torch.from_numpy(c[soc_vars_mask])

    return g

class InstanceDataset(DGLDataset):
    def __init__(self, instances_fpaths, sols_dir='/home/bruno/sat-gnn/data/interim',
                 name='Optimality of Dimensions - Instance', split='train',
                 return_model=False, **kwargs):
        super().__init__(name, **kwargs)

        sols_dir = Path(sols_dir)
        assert sols_dir.exists()

        i_range = torch.arange(150)
        if split.lower() == 'train':
            i_range = i_range[:60]
        elif split.lower() == 'val':
            i_range = i_range[60:80]
        elif split.lower() == 'test':
            i_range = i_range[80:]

        models = list()
        self.targets = list()
        self.gs = list()
        for instance_fp in instances_fpaths:
            i = int(instance_fp.name[:-len('.json')].split('_')[-1])
            if i not in i_range:  # instance is not part of the split
                continue

            with open(instance_fp) as f:
                instance = json.load(f)

            sol_fp = sols_dir/instance_fp.name.replace('.json', '_sols.npz')
            if not sol_fp.exists():
                print('solutions were not computed for ', instance_fp)
                continue
            sol_npz = np.load(sol_fp)
            sols_objs = sol_npz['arr_0'], sol_npz['arr_1']

            m = get_model(instance, coupling=True, new_ineq=False)

            self.gs.append(make_graph_from_model(m))
            self.targets.append(sols_objs)

            models.append(m)
        
        if return_model:
            self.models = models
        else:
            del models

    def __len__(self):
        return len(self.gs)

    def __getitem__(self, idx):
        # g = deepcopy(self.gs[idx])
        g = self.gs[idx]

        ys = self.targets[idx]

        try:
            m = self.models[idx]
            return g, ys, m
        except AttributeError:
            return g, ys
