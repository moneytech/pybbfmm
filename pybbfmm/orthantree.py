import torch
from . import sets, ragged
from aljpy import arrdict

def underoccupied(source_idxs, target_idxs, terminal, capacity):
    source_unique, source_counts = torch.unique(source_idxs, return_counts=True)
    target_unique, target_counts = torch.unique(target_idxs, return_counts=True)

    source_okay = torch.ones_like(terminal)
    source_okay[source_unique] = (source_counts <= capacity)
    target_okay = torch.ones_like(terminal)
    target_okay[target_unique] = (target_counts <= capacity)

    return source_okay & target_okay


def orthantree(scaled, capacity=8):
    #TODO: Well this is a travesty of incomprehensibility. Verify it then explain yourself.
    D = scaled.sources.shape[1]

    points = torch.cat([scaled.sources, scaled.targets])
    indices = points.new_zeros((len(points),), dtype=torch.long)

    tree = arrdict.arrdict(
        parents=indices.new_full((1,), -1),
        depths=indices.new_zeros((1,)),
        centers=points.new_zeros((1, D)),
        terminal=indices.new_ones((1,), dtype=torch.bool),
        children=indices.new_full((1,) + (2,)*D, -1),
        descent=indices.new_zeros((1, D)))

    bases = 2**torch.flip(torch.arange(D, device=indices.device), (0,))
    subscript_offsets = sets.cartesian_product(torch.tensor([0, 1], device=indices.device), D)
    center_offsets = sets.cartesian_product(torch.tensor([-1, +1], device=indices.device), D)

    depth = 0
    while True:
        used, used_inv = torch.unique(indices, return_inverse=True)
        source_idxs, target_idxs = indices[:len(scaled.sources)], indices[-len(scaled.targets):]
        tree.terminal[used] = underoccupied(source_idxs, target_idxs, tree.terminal, capacity)[used] 
        
        used_is_active = ~tree.terminal[used]
        point_is_active = used_is_active[used_inv]
        if not point_is_active.any():
            break

        depth += 1
        
        active = used[used_is_active]
        active_inv = (used_is_active.cumsum(0) - used_is_active.long())[used_inv[point_is_active]]
        first_child = len(tree.parents) + 2**D*torch.arange(len(active), device=active.device)
        point_offset = ((points[point_is_active] >= tree.centers[active][active_inv])*bases).sum(-1)
        child_box = first_child[active_inv] + point_offset
        indices[point_is_active] = child_box

        trailing_ones = (slice(None),) + (None,)*D
        tree.children[active] = first_child[trailing_ones] + (subscript_offsets*bases).sum(-1)

        centers = tree.centers[active][trailing_ones] + center_offsets.float()/2**depth
        descent = center_offsets[None].expand_as(centers)

        n_children = len(active)*2**D
        children = arrdict.arrdict(
            parents=active.repeat_interleave(2**D),
            depths=tree.depths.new_full((n_children,), depth),
            centers=centers.reshape(-1, D),
            descent=descent.reshape(-1, D),
            terminal=tree.terminal.new_ones((n_children,)),
            children=tree.children.new_full((n_children,) + (2,)*D, -1))
        tree = arrdict.cat([tree, children])

    tree['id'] = torch.arange(len(tree.parents), device=points.device)

    indices = arrdict.arrdict(
        sources=indices[:len(scaled.sources)],
        targets=indices[-len(scaled.targets):])

    return tree, indices

def child_boxes(tree, indices, descent):
    subscripts = (descent + 1)/2
    return tree.children[(indices, *subscripts.T)]

def neighbour_boxes(tree, indices, directions):
    #TODO: This can be framed as a recursive scheme and then as a dynamic programming scheme. 
    # Should save a factor of log(n)
    indices = torch.as_tensor(indices, dtype=tree.parents.dtype, device=tree.parents.device)
    directions = torch.as_tensor(directions, dtype=tree.parents.dtype, device=tree.parents.device)
    directions = directions[None].repeat_interleave(len(indices), 0) if directions.ndim == 1 else directions
    assert len(directions) == len(indices), 'There should be as many directions as indices'

    current = indices.clone()
    alive = [torch.ones_like(indices, dtype=torch.bool)]
    neighbour_descents = []
    while alive[-1].any():
        live = alive[-1] & (directions != 0).any(-1) & (current >= 0)
        alive.append(live)

        descent = tree.descent[current]
        neighbour_descents.append(descent*(1 - 2*directions.abs()))

        directions = (descent + directions).div(2).long() 
        current[live] = tree.parents[current[live]]

    for descent, live in zip(neighbour_descents[::-1], alive[::-1]):
        internal = ~tree.terminal[current] & (current >= 0) & live
        current[internal] = child_boxes(tree, current[internal], descent[internal])

    return current

def u_pairs(tree, neighbours):
    """For childless boxes, the neighbouring childless boxes"""
    pairs = torch.stack([tree.id[:, None].expand_as(neighbours), neighbours], -1)
    pairs = pairs[(pairs >= 0).all(-1) & tree.terminal[pairs].all(-1)]
    pairs, _ = sets.unique_rows(pairs)

    partner_is_larger = tree.depths[pairs[:, 0]] > tree.depths[pairs[:, 1]]
    smaller_partners = torch.flip(pairs[partner_is_larger], (1,))
    pairs = torch.cat([pairs, smaller_partners])
    return pairs

def u_ragged(tree, neighbours):
    unique_neighbours = torch.sort(neighbours, 1, descending=True).values
    unique_neighbours[:, 1:][unique_neighbours[:, 1:] == unique_neighbours[:, :-1]] = -1

    pairs = torch.stack([tree.id[:, None].expand_as(neighbours), unique_neighbours], -1)
    pairs = pairs[(pairs >= 0).all(-1) & tree.terminal[pairs].all(-1)]

    partner_is_larger = tree.depths[pairs[:, 0]] > tree.depths[pairs[:, 1]]
    smaller_partners = torch.flip(pairs[partner_is_larger], (1,))
    pairs = torch.cat([pairs, smaller_partners])

    return ragged.from_pairs(pairs, len(tree.id), len(tree.id))

def v_pairs(tree, directions, neighbours):
    """Children of the parent's colleagues that are separated from the box"""
    D = tree.children.ndim-1
    bs = tree.id
    nonzero = (directions != 0).any(-1)
    friends_descents = sets.flat_cartesian_product(torch.tensor([-1, +1], device=bs.device), D)

    # The v list is many times bigger than the other lists, so we'll go one direction at a time
    # to preserve memory.
    pairs, inverse, offset_depths = [], [], []
    for i in nonzero.nonzero().squeeze(1):
        colleagues = neighbours[tree.parents[tree.id], i]
        friends = torch.stack([child_boxes(tree, colleagues, d) for d in friends_descents], -1)

        own_descents = tree.descent[bs]
        offsets = -own_descents[:, None] + 4*directions[i] + friends_descents[None, :]
        friends[(offsets.abs() <= 2).all(-1)] = -1

        ps = torch.stack([bs[:, None].expand_as(friends), friends], -1)
        ps = ps[friends != -1]
        offsets = offsets[friends != -1]

        offset_depth = torch.cat([offsets, tree.depths[ps[:, 0], None]], -1)
        offset_depth, inv = sets.unique_rows(offset_depth)

        pairs.append(ps)
        inverse.append(inv + sum(map(len, offset_depths)))
        offset_depths.append(offset_depth)
        
    vectors = arrdict.arrdict(
        inverse=torch.cat(inverse),
        offsets=torch.cat(offset_depths)[:, :2],
        depths=torch.cat(offset_depths)[:, 2])

    # No need to assure uniqueness; if a parent's colleague has a child, then it'll necessarily only turn up in
    # the neighbours list once.
    return torch.cat(pairs), vectors

def w_pairs(tree, directions, neighbours):
    """For childless boxes, descendents of colleagues whose parents are adjacent but
    which aren't themselves"""
    D = tree.children.ndim-1
    bs = tree.terminal.nonzero().squeeze(1)

    origins, colleagues, dirs = [], [], []
    for d, dns in zip(directions, neighbours.T):
        dns = dns[bs]
        is_colleague = (tree.depths[bs] == tree.depths[dns])
        valid = is_colleague & ~tree.terminal[dns]
        origins.append(bs[valid])
        colleagues.append(dns[valid])
        dirs.append(d[None].repeat_interleave(valid.sum(), 0))
    origins, colleagues, dirs = torch.cat(origins), torch.cat(colleagues), torch.cat(dirs, 0)

    pairs = [origins.new_empty((0, 2))]
    parents = colleagues
    while parents.nelement():
        friends = tree.children[parents].reshape(-1, 2**D)
        distant = (tree.descent[friends] == dirs[:, None, :]).any(-1)
        
        ps = torch.stack([origins[:, None].expand_as(friends), friends], -1)
        pairs.append(ps[distant])
        
        mask = ~distant & ~tree.terminal[friends]
        origins, parents = ps[mask].T
        dirs = dirs[:, None].repeat_interleave(2**D, 1)[mask]
    pairs = torch.cat(pairs)

    pairs, _ = sets.unique_rows(pairs)
    return pairs

def interaction_scheme(tree):
    D = tree.children.ndim-1
    directions = sets.flat_cartesian_product(torch.tensor([-1, 0, +1], device=tree.id.device), D)
    neighbours = torch.stack([neighbour_boxes(tree, tree.id, d) for d in directions], -1)

    u = u_pairs(tree, neighbours)
    v, v_vectors = v_pairs(tree, directions, neighbours)
    w = w_pairs(tree, directions, neighbours)
    x = w.flip((1,))

    return arrdict.arrdict(
        lists=arrdict.arrdict(v=v, u=u, w=w, x=x), 
        u_ragged=u_ragged(tree, neighbours),
        v_vectors=v_vectors)

## TEST

def y_pairs(tree, b):
    """Everything well-separated from the parent
    
    This isn't used in production, it's just for debugging. It should equal the compliment of the other lists"""
    D = tree.children.ndim-1
    ds = sets.flat_cartesian_product(torch.tensor([-1, 0, +1], device=tree.id.device), D)

    colleagues = torch.cat([neighbour_boxes(tree, tree.parents[[b]], d) for d in ds])

    # A leaf is well-separated from b's parent if it's not a descendent of the colleagues.
    descendents = [colleagues]
    while descendents[-1].nelement():
        children = tree.children[descendents[-1]]
        parents = children[children >= 0]
        descendents.append(parents)
    descendents = torch.cat(descendents)
    leaves = tree.terminal.nonzero().squeeze(1)
    ys = leaves[~(leaves[:, None] == descendents[None, :]).any(-1)]
    return ys

def ancestor_interactions(tree, lists, b):
    import pandas as pd

    ancestors = [torch.as_tensor([b], device=tree.id.device)]
    while ancestors[-1].nelement():
        parents = tree.parents[ancestors[-1]]
        ancestors.append(parents[parents >= 0])
    ancestors = torch.cat(ancestors)

    ixns = []
    for height, ancestor in enumerate(ancestors):
        for k, l in lists.items():
            kl_ixns = l[:, 1][l[:, 0] == ancestor]
            for ixn in kl_ixns:
                ixns.append((height, int(ancestor), k, int(ixn)))
    return pd.DataFrame(ixns, columns=['height', 'ancestor', 'list', 'partner'])

def terminal_descendents(tree, bs):
    bs = torch.as_tensor(bs, device=tree.id.device) 

    terminal = [bs[tree.terminal[bs]]]
    parents = bs[~tree.terminal[bs]]
    while parents.nelement():
        children = tree.children[parents].flatten()
        parents = children[(children >= 0) & ~tree.terminal[children]]
        terminal.append(children[(children >= 0) & tree.terminal[children]])
    return torch.cat(terminal)

def test_lists(tree):
    # Generate a random problem
    # Get the tree
    # Get the lists
    # Check that the partners of each box and its ancestors partition the grid
    scheme = interaction_scheme(tree)
    bs = tree.terminal.nonzero().squeeze()
    for b in bs:
        print(f'Checking {b}')
        ixns = ancestor_interactions(tree, scheme.lists, b)
        terminal = terminal_descendents(tree, ixns.partner.values)

        assert ixns.partner.value_counts().max() <= 1
        assert tree.terminal.sum() == len(terminal)