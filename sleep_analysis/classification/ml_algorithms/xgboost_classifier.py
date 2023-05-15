import numpy as np
import optuna
from optuna.integration import XGBoostPruningCallback
from optuna.samplers import TPESampler
from sklearn.base import clone
from tpcp import OptimizableParameter, OptimizablePipeline, cf
from tpcp.optimize import Optimize
from tpcp.validate import cross_validate
from xgboost import XGBClassifier

from sleep_analysis.classification.utils.utils import get_db_path
from sleep_analysis.datasets.mesadataset import MesaDataset


class XGBPipeline(OptimizablePipeline):
    classifier: OptimizableParameter

    def __init__(
        self,
        modality,
        n_estimators=100,
        max_depth=10,
        reg_alpha=0,
        reg_lambda=0,
        min_child_weight=0,
        gamma=0.0,
        learning_rate=0.005,
        colsample_bytree=0.1,
        classifier=cf(XGBClassifier(n_jobs=-1)),
        classification_type="binary",
    ):
        self.classifier = classifier
        self.modality = modality
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.reg_alpha = reg_alpha
        self.reg_lambda = reg_lambda
        self.min_child_weight = min_child_weight
        self.gamma = gamma
        self.learning_rate = learning_rate
        self.colsample_bytree = colsample_bytree
        self.classification_type = classification_type

        self.epoch_length = 30
        self.algorithm = "xgb"

    def self_optimize(self, dataset: MesaDataset, **kwargs):
        """
        Optimization of whole trainset
        :param dataset: Dataset instance representing the whole train set with its sleep data
        """
        # Concat whole dataset to one DataFrame
        features, ground_truth = dataset.get_concat_dataset(dataset, modality=self.modality)

        # Set classifier parameters from Optuna Optimization
        c = self._set_classifier_params(clone(self.classifier))

        # Train classifier
        if self.classification_type == "binary":
            self.classifier = c.fit(np.ascontiguousarray(features), np.ascontiguousarray(ground_truth["sleep"]))
        else:
            self.classifier = c.fit(
                np.ascontiguousarray(features), np.ascontiguousarray(ground_truth[self.classification_type])
            )
        return self

    def _set_classifier_params(self, classifier):
        params = {
            "n_estimators": self.n_estimators,
            "max_depth": self.max_depth,
            "reg_alpha": self.reg_alpha,
            "reg_lambda": self.reg_lambda,
            "min_child_weight": self.min_child_weight,
            "gamma": self.gamma,
            "learning_rate": self.learning_rate,
            "colsample_bytree": self.colsample_bytree,
        }
        return classifier.set_params(**params)

    def run(self, datapoint: MesaDataset):
        """
        Subject-wise classification based on trained model
        :param datapoint: Dataset instance representing the sleep data of one participant
        """
        features = datapoint.get_features(datapoint, modality=self.modality)

        self.classification_ = self.classifier.predict(np.ascontiguousarray(features))

        return self


from tpcp.optimize.optuna import CustomOptunaOptimize


class XGBOptuna(CustomOptunaOptimize):
    def __init__(self, pipeline, score_function, modality, classification_type="binary", seed=1):
        self.pipeline = pipeline
        self.score_function = score_function
        self.res = {}
        self.seed = seed
        self.modality = modality
        self.classification_type = classification_type

    def optimize(self, dataset: MesaDataset, **optimize_params):
        """Apply optuna optimization on the input parameters of the pipeline."""

        def objective(trial):
            paras_to_be_searched = {
                "n_estimators": trial.suggest_int("n_estimators", 200, 400),
                "max_depth": trial.suggest_int("max_depth", 5, 25),
                "reg_alpha": trial.suggest_int("reg_alpha", 0, 25),
                "reg_lambda": trial.suggest_int("reg_lambda", 0, 25),
                "min_child_weight": trial.suggest_int("min_child_weight", 0, 25),
                "gamma": trial.suggest_int("gamma", 5, 25),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.1, 1),
            }

            # Clone the pipeline, so it will not change the original pipeline
            # Set the parameters that need to be searched in this cloned pipeline
            temp_pipeline = self.pipeline.clone().set_params(**paras_to_be_searched)

            pruning_callback = XGBoostPruningCallback(trial, "test_mcc")

            # Run cross validation
            results = cross_validate(
                optimizable=Optimize(temp_pipeline),
                dataset=dataset,
                cv=5,
                optimize_params={"early_stopping_rounds": 10, "pruning_callback": pruning_callback},
                scoring=self.score_function,
                return_optimizer=True,
                n_jobs=-1,
            )

            return np.mean(results["test_mcc"])

        # Create and run an optuna study + save trial information
        db_path = get_db_path()
        study = optuna.create_study(
            direction="maximize",
            study_name="XGBoostOptuna" + "|".join(self.modality),
            sampler=TPESampler(seed=self.seed),
            storage="sqlite:////"
            + db_path
            + "/xgb_"
            + "_".join(self.modality)
            + "_"
            + self.classification_type
            + ".db",
            load_if_exists=True,
            pruner=optuna.pruners.MedianPruner,
        )

        study.optimize(objective, n_trials=1, show_progress_bar=True)

        best_parameters = study.best_params
        if self.classification_type == "binary":
            best_parameters["classifier"] = XGBClassifier(objective="binary:logistic", **study.best_params)
        else:
            best_parameters["classifier"] = XGBClassifier(**study.best_params)

        # Set the best params in a new cloned pipeline, refit it and save it
        self.optimized_pipeline_ = (
            Optimize(self.pipeline.clone().set_params(**best_parameters))
            .optimize(dataset, **(optimize_params or {}))
            .optimized_pipeline_
        )

        return self
