from abc import ABC, abstractmethod

import numpy as np
from numpy.random import RandomState
from scipy.sparse import csc_matrix, csr_matrix

from bayesian_decision_tree.utils import hypercube_to_hypersphere_surface, r2_series_generator


class HyperplaneOptimizationFunction:
    """The function to optimize for hyperplane trees. This is a function of `n_dim` variables representing
    the normal vector of a hyperplane in `n_dim` dimensions. Given such a hyperplane normal the function
    computes the optimum split location (i.e., the origin of the hyperplane) in the data such that the
    data likelihood is maximized.
    """

    def __init__(
        self,
        X,
        y,
        prior,
        compute_log_p_data_split,
        log_p_data_no_split,
        search_space_is_unit_hypercube,
        split_precision,
    ):
        self.X = X
        self.y = y
        self.prior = prior
        self.compute_log_p_data_split = compute_log_p_data_split
        self.log_p_data_no_split = log_p_data_no_split
        self.search_space_is_unit_hypercube = search_space_is_unit_hypercube
        self.split_precision = split_precision

        # results of the optimization - to be set later during the actual optimization
        self.function_evaluations = 0
        self.best_log_p_data_split = log_p_data_no_split
        self.best_cumulative_distances = 0
        self.best_hyperplane_normal = None
        self.best_hyperplane_origin = None

    def compute(self, hyperplane_normal):
        self.function_evaluations += 1

        if self.search_space_is_unit_hypercube:
            hyperplane_normal = hypercube_to_hypersphere_surface(hyperplane_normal, half_hypersphere=True)

        # catch some special cases and normalize to unit length
        hyperplane_normal = np.nan_to_num(hyperplane_normal)
        if np.all(hyperplane_normal == 0):
            hyperplane_normal[0] = 1

        hyperplane_normal /= np.linalg.norm(hyperplane_normal)

        dense = isinstance(self.X, np.ndarray)
        if not dense and isinstance(self.X, csr_matrix):
            self.X = csc_matrix(self.X)

        # compute distance of all points to the hyperplane: https://mathinsight.org/distance_point_plane
        projections = self.X @ hyperplane_normal  # up to an additive constant which doesn't matter to distance ordering
        sort_indices = np.argsort(projections)
        split_indices = (
            1 + np.where(np.abs(np.diff(projections)) > self.split_precision)[0]
        )  # we can only split between *different* data points
        if len(split_indices) == 0:
            # no split possible along this dimension
            return -self.log_p_data_no_split

        y_sorted = self.y[sort_indices]

        # compute data likelihoods of all possible splits along this projection and find split with highest data likelihood
        n_dim = self.X.shape[1]
        log_p_data_split = self.compute_log_p_data_split(y_sorted, self.prior, n_dim, split_indices)
        i_max = log_p_data_split.argmax()
        if log_p_data_split[i_max] >= self.best_log_p_data_split:
            best_split_index = split_indices[i_max]
            p1 = self.X[sort_indices[best_split_index - 1]]
            p2 = self.X[sort_indices[best_split_index]]
            if not dense:
                p1 = p1.toarray()[0]
                p2 = p2.toarray()[0]

            hyperplane_origin = 0.5 * (p1 + p2)  # middle between the points that are being split
            projections_with_origin = projections - np.dot(hyperplane_normal, hyperplane_origin)
            cumulative_distances = np.sum(np.abs(projections_with_origin))

            if log_p_data_split[i_max] > self.best_log_p_data_split:
                is_log_p_better_or_same_but_with_better_distance = True
            else:
                # accept new split with same log(p) only if it increases the cumulative distance of all points to the hyperplane
                is_log_p_better_or_same_but_with_better_distance = cumulative_distances > self.best_cumulative_distances

            if is_log_p_better_or_same_but_with_better_distance:
                self.best_log_p_data_split = log_p_data_split[i_max]
                self.best_cumulative_distances = cumulative_distances
                self.best_hyperplane_normal = hyperplane_normal
                self.best_hyperplane_origin = hyperplane_origin

        return -log_p_data_split[i_max]


class StrMixin:
    """Auto-generate `__str__()` and `__repr__()` from attributes."""

    def __str__(self):
        attributes = [f"{k}={v}" for k, v in self.__dict__.items()]
        return "{}[{}]".format(type(self).__name__, ", ".join(attributes))

    def __repr__(self):
        return self.__str__()


class HyperplaneOptimizer(ABC, StrMixin):
    """Abstract base class of all hyperplane optimizers."""

    def __init__(self, search_space_is_unit_hypercube):
        self.search_space_is_unit_hypercube = search_space_is_unit_hypercube

    @abstractmethod
    def solve(self, optimization_function):
        raise NotImplementedError


class ScipyOptimizer(HyperplaneOptimizer):
    """An optimizer using one of the scipy global optimizers, see [1].

    References:
    ----------
    .. [1] https://docs.scipy.org/doc/scipy/reference/optimize.html#global-optimization
    """

    def __init__(self, solver_type, seed, **extra_solver_kwargs):
        super().__init__(search_space_is_unit_hypercube=True)

        self.solver_type = solver_type
        self.seed = seed
        self.extra_solver_kwargs = extra_solver_kwargs

    def solve(self, optimization_function):
        # bounds for scipy optimizers: unit hypercube (will be mapped to
        # (half) hypersphere uniformly later on)
        X = optimization_function.X
        n_dim = X.shape[1]
        unit_hypercube_bounds = np.vstack((np.zeros(n_dim - 1), np.ones(n_dim - 1))).T

        solver = self.solver_type(
            func=optimization_function.compute, bounds=unit_hypercube_bounds, **self.extra_solver_kwargs
        )

        solver.solve()


class RandomTwoPointOptimizer(HyperplaneOptimizer):
    """An optimizer randomly choosing two points of different classes to construct
    a bisecting hyperplane (experimental).
    TODO: Complete
    """

    def __init__(self, n_mc, seed):
        super().__init__(search_space_is_unit_hypercube=False)

        self.n_mc = n_mc
        self.seed = seed

    def solve(self, optimization_function):
        rand = RandomState(self.seed)

        X = optimization_function.X
        y = optimization_function.y

        if np.any(np.round(y) != y):
            raise TypeError(
                f"Cannot use {RandomTwoPointOptimizer.__name__} for regression problems as there are no classes to pick points from"
            )

        dense = isinstance(X, np.ndarray)

        if len(set(y)) <= 1:
            # can't pick two points of different classes if there aren't at least two classes
            return

        # find indices of each class
        n_classes = int(y.max()) + 1
        class_indices = [np.where(y == i)[0] for i in range(n_classes)]

        # evaluate 'n_mc' hyperplane normals passing through two random points form different classes
        for i in range(self.n_mc):
            indices1 = []
            indices2 = []

            while len(indices1) == 0 or len(indices2) == 0:
                class1 = rand.randint(0, n_classes)
                indices1 = class_indices[class1]

                class2 = class1
                while class2 == class1:
                    class2 = rand.randint(0, n_classes)

                indices2 = class_indices[class2]

            p1 = X[indices1[rand.randint(0, len(indices1))]]
            p2 = X[indices2[rand.randint(0, len(indices2))]]
            if not dense:
                p1 = p1.toarray()[0]
                p2 = p2.toarray()[0]

            normal = p2 - p1
            if normal[0] < 0:
                normal *= -1  # make sure the first coordinate is positive to match the scipy search space

            optimization_function.compute(normal)


class RandomHyperplaneOptimizer(HyperplaneOptimizer):
    """An optimizer generating hyperplanes with random orientation
    in space (experimental).
    TODO: Complete
    """

    def __init__(self, n_mc, seed):
        super().__init__(search_space_is_unit_hypercube=False)

        self.n_mc = n_mc
        self.seed = seed

    def solve(self, optimization_function):
        rand = RandomState(self.seed)

        X = optimization_function.X
        n_dim = X.shape[1]

        for i in range(self.n_mc):
            hyperplane_normal = rand.normal(0, 1, n_dim)
            optimization_function.compute(hyperplane_normal)


class QuasiRandomHyperplaneOptimizer(HyperplaneOptimizer):
    """An optimizer generating hyperplanes with quasi-random orientation
    in space, see
    http://extremelearning.com.au/unreasonable-effectiveness-of-quasirandom-sequences/
    """

    def __init__(self, n):
        super().__init__(search_space_is_unit_hypercube=True)

        self.n = n

    def solve(self, optimization_function):
        X = optimization_function.X
        n_dim = X.shape[1]
        n_dim_surface = n_dim - 1

        # quasi-random R2 sequence
        r2gen = r2_series_generator(n_dim_surface)
        for i in range(self.n):
            uniform = next(r2gen)
            optimization_function.compute(uniform)


class OptunaOptimizer(HyperplaneOptimizer):
    def __init__(self, n_trials, seed):
        super().__init__(search_space_is_unit_hypercube=True)

        self.n_trials = n_trials
        self.seed = seed

    def solve(self, optimization_function):
        from optuna import create_study
        from optuna.logging import set_verbosity
        from optuna.samplers import TPESampler

        study = create_study(direction="minimize", sampler=TPESampler(self.seed))
        n_dim = optimization_function.X.shape[1]
        n_dim_surface = n_dim - 1

        def objective(trial):
            uniform = np.zeros(n_dim_surface)
            for i in range(n_dim_surface):
                uniform[i] = trial.suggest_uniform(f"uniform[{i}]", 0, 1)

            return optimization_function.compute(uniform)

        set_verbosity(0)
        study.optimize(objective, n_trials=self.n_trials)


class SimulatedAnnealingOptimizer(HyperplaneOptimizer):
    """A simple simulated annealing optimizer (experimental).
    TODO: Complete
    """

    def __init__(self, n_scan, n_keep, spread_factor, seed):
        super().__init__(search_space_is_unit_hypercube=True)

        self.n_scan = n_scan
        self.n_keep = n_keep
        self.spread_factor = spread_factor
        self.seed = seed

    def solve(self, optimization_function):
        rand = RandomState(self.seed)

        X = optimization_function.X
        n_dim = X.shape[1] - 1

        candidates = {}

        no_improvements = 0
        best_value = np.inf

        f = 1
        while no_improvements < 50:
            if len(candidates) == 0:
                # first run
                for i in range(self.n_scan):
                    candidate = rand.uniform(0, 1, n_dim)
                    value = optimization_function.compute(candidate)
                    candidates[value] = candidate
            else:
                # evolution
                vectors = list(candidates.values())
                ranges = [np.max([v[i] for v in vectors]) - np.min([v[i] for v in vectors]) for i in range(n_dim)]

                values_sorted = sorted(candidates.keys())
                best_value = values_sorted[0]
                for i in range(self.n_keep):
                    i_candidate = i * len(values_sorted) // self.n_keep
                    candidate = candidates[values_sorted[i_candidate]]
                    # perturbation = ranges * rand.uniform(-1, 1, len(ranges))
                    perturbation = f * rand.uniform(-1, 1, len(ranges))
                    new_candidate = candidate + perturbation
                    new_candidate = np.clip(new_candidate, 0, 1)
                    value = optimization_function.compute(new_candidate)
                    candidates[value] = new_candidate

                f *= self.spread_factor

            # only keep the best candidates
            values_sorted = sorted(candidates.keys())
            values_sorted = values_sorted[: self.n_keep]
            if values_sorted[0] < best_value:
                no_improvements = 0
            else:
                no_improvements += 1

            candidates = {v: candidates[v] for v in values_sorted}


class GradientDescentOptimizer(HyperplaneOptimizer):
    """A simple gradient descent optimizer (experimental).
    TODO: Complete
    """

    def __init__(self, n_init, n_keep):
        super().__init__(search_space_is_unit_hypercube=True)

        self.n_init = n_init
        self.n_keep = n_keep

    def solve(self, optimization_function):
        X = optimization_function.X
        n_dim = X.shape[1] - 1

        rand = RandomState(666)

        candidates = {}

        no_improvements = 0
        best_value = np.inf

        start_delta = 1e-6
        while no_improvements < 3:
            if len(candidates) == 0:
                # first run
                for i in range(self.n_init):
                    candidate = rand.uniform(0, 1, n_dim)
                    value = optimization_function.compute(candidate)
                    candidates[value] = candidate
            else:
                # compute numerical gradient for each of the best vectors
                values_sorted = sorted(candidates.keys())
                best_value = values_sorted[0]
                for i in range(self.n_keep):
                    i_candidate = i * len(values_sorted) // self.n_keep
                    value = values_sorted[i_candidate]
                    candidate = candidates[value]

                    gradient = np.zeros(n_dim)
                    delta = start_delta

                    while True:
                        delta_too_small = False

                        for i_dim in range(n_dim):
                            new_candidate = candidate.copy()
                            new_candidate[i_dim] += delta
                            if new_candidate[i_dim] > 1:
                                delta *= -1
                                new_candidate[i_dim] = candidate[i_dim] + delta

                            new_value = optimization_function.compute(new_candidate)
                            gradient[i_dim] = (new_value - value) / delta
                            delta = np.abs(delta)
                            if gradient[i_dim] == 0:
                                delta_too_small = True
                                break

                        if delta_too_small:
                            delta *= 10
                            if delta >= 1:
                                # can't compute gradient, so give up
                                break
                        else:
                            break

                    if delta_too_small:
                        continue

                    start_delta = delta / 10

                    # add gradient to vector
                    lambda_ = 1e-6
                    best_new_candidate = candidate
                    best_new_value = value
                    while True:
                        new_candidate = candidate - lambda_ * gradient
                        new_candidate = np.clip(new_candidate, 0, 1)
                        new_value = optimization_function.compute(new_candidate)
                        if new_value < best_new_value:
                            lambda_ *= 2
                            best_new_candidate = new_candidate
                            best_new_value = new_value
                        else:
                            break

                    candidates[best_new_value] = best_new_candidate

            # only keep the best candidates
            values_sorted = sorted(candidates.keys())
            values_sorted = values_sorted[: self.n_keep]
            if values_sorted[0] < best_value:
                no_improvements = 0
            else:
                no_improvements += 1

            candidates = {v: candidates[v] for v in values_sorted}
