from abc import ABC, abstractmethod

import numpy as np
import pandas as pd
from scipy.sparse import csc_matrix, csr_matrix
from sklearn.base import BaseEstimator


class BaseTree(ABC, BaseEstimator):
    """Abstract base class of all Bayesian decision tree models (classification and regression). Performs all
    high-level fitting and prediction tasks and outsources the medium- and low-level work to subclasses.

    Implementation note: This class hierarchy is diamond-shaped: The four concrete model classes each
    inherit from two superclasses which in turn inherit from this class.
    """

    def __init__(self, partition_prior, prior, delta, prune, child_type, is_regression, split_precision, level):
        self.partition_prior = partition_prior
        self.prior = prior
        self.delta = delta
        self.prune = prune
        self.child_type = child_type
        self.is_regression = is_regression
        self.split_precision = split_precision
        self.level = level

    def fit(self, X, y, verbose=False, feature_names=None):
        """Trains this classification or regression tree using the training set (X, y).

        Parameters
        ----------
        X : array-like, scipy.sparse.csc_matrix, scipy.sparse.csr_matrix or pandas.DataFrame, shape = [n_samples, n_features]
            The training input samples.

        y : array-like, shape = [n_samples] or [n_samples, n_outputs]
            The target values. In case of binary classification only the
            integers 0 and 1 are permitted. In case of multiclass classification
            only the integers 0, 1, ..., {n_classes-1} are permitted. In case of
            regression all finite float values are permitted.

        verbose : bool, default=False
            Prints fitting progress.

        feature_names: array-lie, shape = [n_features]
            An optional sequence of feature names. If not provided then 'x0', 'x1', ... is used
            if X is a matrix, or the column headers if X is a DataFrame.

        References:
        ----------

        .. [1] https://arxiv.org/abs/1901.03214
        """
        # validation and input transformation
        if isinstance(y, list):
            y = np.array(y)

        y = y.squeeze()
        y = self._ensure_float64(y)
        self._check_target(y)

        X, feature_names = self._normalize_data_and_feature_names(X, feature_names)
        if X.shape[0] != len(y):
            raise ValueError(f"Invalid shapes: X={X.shape}, y={y.shape}")

        # fit
        self._fit(X, y, verbose, feature_names, "root")

        if self.prune:
            self._prune()

        return self

    def predict(self, X):
        """Predict class or regression value for X.

        For a classification model, the predicted class for each sample in X is
        returned. For a regression model, the predicted value based on X is
        returned.

        Parameters
        ----------
        X : array-like, scipy.sparse.csc_matrix, scipy.sparse.csr_matrix or pandas.DataFrame, shape = [n_samples, n_features]
            The input samples.

        Returns:
        -------
        y : array of shape = [n_samples]
            The predicted classes, or the predict values.
        """
        # input transformation and checks
        X, _ = self._normalize_data_and_feature_names(X)
        self._ensure_is_fitted(X)

        indices = np.arange(X.shape[0])
        y = np.zeros(X.shape[0])
        self._predict(X, indices, True, y)

        return y

    def feature_importance(self):
        """Compute and return feature importance of this tree after having fitted it to data. Feature
        importance for a given feature dimension is defined as the sum of all increases in the
        marginal data log-likelihood across splits of that dimension. Finally, the feature
        importance vector is normalized to sum to 1.

        Returns:
        -------
        feature_importance: array of floats
            The feature importance.
        """
        self._ensure_is_fitted()

        feature_importance = np.zeros(self.n_dim_)
        self._update_feature_importance(feature_importance)
        feature_importance /= feature_importance.sum()

        return feature_importance

    def _predict(self, X, indices, predict_class, y):
        if self.is_leaf():
            prediction = (
                self._get_raw_leaf_data_internal()
                if predict_class is None
                else self._predict_leaf()
                if predict_class
                else self._compute_posterior_mean()
            )
            for i in indices:
                y[i] = prediction
        else:
            dense = isinstance(X, np.ndarray)
            if not dense and isinstance(X, csr_matrix):
                # column accesses coming up, so convert to CSC sparse matrix format
                X = csc_matrix(X)

            # query both children, let them predict their side, and then re-assemble
            indices1, indices2 = self._compute_child1_and_child2_indices(X, indices, dense)

            if len(indices1) > 0:
                self.child1_._predict(X, indices[indices1], predict_class, y)

            if len(indices2) > 0:
                self.child2_._predict(X, indices[indices2], predict_class, y)

    def _prune(self):
        if self.is_leaf():
            return

        depth_start = self.get_depth()
        n_leaves_start = self.get_n_leaves()

        if self.child1_.is_leaf() and self.child2_.is_leaf():
            if self.child1_._predict_leaf() == self.child2_._predict_leaf():
                # same prediction (class if classification, value if regression) -> no need to split
                self._erase_split_info_base()
                self._erase_split_info()
        else:
            self.child1_._prune()
            self.child2_._prune()

        if depth_start != self.get_depth() or n_leaves_start != self.get_n_leaves():
            # we did some pruning somewhere down this sub-tree -> prune again
            self._prune()

    def _get_raw_leaf_data(self, X):
        """Returns the raw predicted leaf data.

        For both classification and regression models, the following  data is returned for each row of X:
        [[prior], [posterior]]. This method directly accesses implementation details and should therefore
        be used with caution.

        Parameters
        ----------
        X : array-like, scipy.sparse.csc_matrix, scipy.sparse.csr_matrix or pandas.DataFrame, shape = [n_samples, n_features]
            The input samples.

        Returns:
        -------
        y : array of shape = [n_samples * 2 * n_classes] for classification problems
            or [n_samples * 2 * 4] for regression problems.
        """
        # input transformation and checks
        X, _ = self._normalize_data_and_feature_names(X)
        self._ensure_is_fitted(X)

        indices = np.arange(X.shape[0])
        raw_leaf_data = [None] * X.shape[0]
        self._predict(X, indices, None, raw_leaf_data)

        return np.array(raw_leaf_data)

    @abstractmethod
    def _update_feature_importance(self, feature_importance):
        raise NotImplementedError

    @staticmethod
    def _normalize_data_and_feature_names(X, feature_names=None):
        if isinstance(X, pd.DataFrame):
            if feature_names is None:
                feature_names = X.columns

            X = X.values
        else:
            if isinstance(X, list):
                X = np.array(X)
            elif np.isscalar(X):
                X = np.array([X])

            if X.ndim == 1:
                X = np.expand_dims(X, 0)

            if feature_names is None:
                feature_names = [f"x{i}" for i in range(X.shape[1])]

        X = BaseTree._ensure_float64(X)

        if X.ndim != 2:
            raise ValueError(f"X should have 2 dimensions but has {X.ndim}")

        return X, feature_names

    @staticmethod
    def _ensure_float64(data):
        if data.dtype in (
            np.int8,
            np.int16,
            np.int32,
            np.int64,
            np.uint8,
            np.uint16,
            np.uint32,
            np.uint64,
            np.float32,
            np.float64,
        ):
            return data

        # convert to np.float64 for performance reasons (matrices with floats but of type object are very slow)
        X_float = data.astype(np.float64)
        if not np.all(data == X_float):
            raise ValueError(
                "Cannot convert data matrix to np.float64 without loss of precision. Please check your data."
            )

        return X_float

    def _ensure_is_fitted(self, X=None):
        if not self.is_fitted():
            raise ValueError("Cannot predict on an untrained model; call .fit() first")

        if X is not None and X.shape[1] != self.n_dim_:
            raise ValueError(f"Bad input dimensions: Expected {self.n_dim_}, got {X.shape[1]}")

    def is_fitted(self):
        return hasattr(self, "posterior_")

    def get_depth(self):
        """Computes and returns the tree depth.

        Returns:
        -------
        depth : int
            The tree depth.
        """
        return self._update_depth(0)

    def get_n_leaves(self):
        """Computes and returns the total number of leaves of this tree.

        Returns:
        -------
        n_leaves : int
            The number of leaves.
        """
        return self._update_n_leaves(0)

    def _update_depth(self, depth):
        if self.is_leaf():
            return max(depth, self.level)
        if self.child1_ is not None:
            depth = self.child1_._update_depth(depth)
            depth = self.child2_._update_depth(depth)

        return depth

    def _update_n_leaves(self, n_leaves):
        if self.is_leaf():
            return n_leaves + 1
        if self.child1_ is not None:
            n_leaves = self.child1_._update_n_leaves(n_leaves)
            n_leaves = self.child2_._update_n_leaves(n_leaves)

        return n_leaves

    def _erase_split_info_base(self):
        self.child1_ = None
        self.child2_ = None
        self.log_p_data_no_split_ = None
        self.best_log_p_data_split_ = None

    @abstractmethod
    def _get_prior(self, n_data, n_dim):
        raise NotImplementedError

    @abstractmethod
    def _erase_split_info(self):
        raise NotImplementedError

    @abstractmethod
    def is_leaf(self):
        raise NotImplementedError

    @abstractmethod
    def _check_target(self, y):
        raise NotImplementedError

    @abstractmethod
    def _compute_log_p_data_no_split(self, y, prior):
        raise NotImplementedError

    @abstractmethod
    def _compute_log_p_data_split(self, y, prior, n_dim, split_indices):
        raise NotImplementedError

    @abstractmethod
    def _compute_posterior(self, y, prior, delta=1):
        raise NotImplementedError

    @abstractmethod
    def _compute_posterior_mean(self):
        raise NotImplementedError

    @abstractmethod
    def _predict_leaf(self):
        raise NotImplementedError

    @abstractmethod
    def _get_raw_leaf_data_internal(self):
        raise NotImplementedError

    @abstractmethod
    def _fit(self, X, y, verbose, feature_names, side_name):
        raise NotImplementedError

    def __repr__(self):
        return self.__str__()

    @abstractmethod
    def _compute_child1_and_child2_indices(self, X, indices, dense):
        raise NotImplementedError
