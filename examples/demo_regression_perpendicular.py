import numpy as np
from sklearn.metrics import mean_squared_error

from bayesian_decision_tree.regression import PerpendicularRegressionTree
from examples import helper

# demo script for regression using classic, axis-normal splits
if __name__ == "__main__":
    # proxies (in case you're running this behind a firewall)
    args = helper.parse_args()
    proxies = {"http": args.http_proxy, "https": args.https_proxy}

    # data set: uncomment one of the following sections

    # # synthetic sine wave
    # X_train = np.linspace(0, 10, 100).reshape(-1, 1)
    # y_train = 1 * np.sin(np.linspace(0, 10, 100)).reshape(-1, 1)
    # train = np.hstack((X_train, y_train))
    # test = train

    # or, alternatively, load a UCI dataset (where we *regress* on the class labels, i.e., class 1 = 0.0 and class 2 = 1.0)
    train, test = helper.load_ripley(proxies)

    n_dim = len(np.unique(train[:, -1]))

    if train is test:
        # perform a 50:50 train:test split if no test data is given
        train = train[0::2]
        test = test[1::2]

    X_train = train[:, :-1]
    y_train = train[:, -1]
    X_test = test[:, :-1]
    y_test = test[:, -1]

    # prior for regression: Normal-Gamma prior, see https://en.wikipedia.org/wiki/Conjugate_prior#Continuous_distributions
    mu = y_train.mean()
    sd_prior = y_train.std() / 10
    prior_pseudo_observations = 1
    kappa = prior_pseudo_observations
    alpha = prior_pseudo_observations / 2
    var_prior = sd_prior**2
    tau_prior = 1 / var_prior
    beta = alpha / tau_prior
    prior = np.array([mu, kappa, alpha, beta])

    # model
    model = PerpendicularRegressionTree(partition_prior=0.9, prior=prior, delta=0)

    # train
    model.fit(X_train, y_train)
    print(model)
    print()
    print(f"Tree depth and number of leaves: {model.get_depth()}, {model.get_n_leaves()}")
    print("Feature importance:", model.feature_importance())

    # compute RMSE
    rmse_train = np.sqrt(mean_squared_error(model.predict(X_train), y_train))
    rmse_test = np.sqrt(mean_squared_error(model.predict(X_test), y_test))
    info_train = f"RMSE train: {rmse_train:.4f}"
    info_test = f"RMSE test:  {rmse_test:.4f}"
    print(info_train)
    print(info_test)

    # plot if 1D or 2D
    dimensions = X_train.shape[1]
    if dimensions == 1:
        helper.plot_1d_perpendicular(model, X_train, y_train, info_train, X_test, y_test, info_test)
    elif dimensions == 2:
        helper.plot_2d_perpendicular(model, X_train, y_train, info_train, X_test, y_test, info_test)
