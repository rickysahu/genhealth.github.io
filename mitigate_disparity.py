import os
import logging
import argparse
import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd
import fairlearn.metrics
from xgboost import XGBClassifier
from xgboost._typing import ArrayLike
from measure_disparity.measure_disparity import main as measure
from fairlearn.postprocessing import ThresholdOptimizer
from sklearn.metrics import precision_score, accuracy_score
from sklearn.model_selection import train_test_split, GridSearchCV


class PredictBeforeFitError(Exception):
    def __init__(self, *args):  
        super().__init__(self, *args)


class FairModel(object):
    base_model: XGBClassifier
    optimizer: ThresholdOptimizer = None

    def __init__(
            self,
            sensitive_feature_names: list[str],
            **_classifier_kwargs
    ):
        self.sensitive_feature_names = sensitive_feature_names
        self._classifier_kwargs = _classifier_kwargs
        pass

    def fit(
            self,
            x: ArrayLike,
            y: ArrayLike,
            sample_weights: ArrayLike = None,
            scale_pos_weight=None,
            constraints="equalized_odds",
            objective="balanced_accuracy_score",
            predict_method="predict_proba",
            grid_size=10,
            flip=False,
            do_hyperparameter_optimization=False,
            enable_categorical=True,
            **kwargs
    ):

        if not do_hyperparameter_optimization:
            classifier_args = {k: v for k, v in self._classifier_kwargs.items()}
            classifier_args.update({
                'eta': 0.05,
                'learning_rate': 0.01,
                'subsample': 0.07
            })
        else:
            params = {
                'learning_rate': [0.01, 0.1, 0.2, 0.3, 0.4],
                'subsample': np.arange(0.01, 0.1, 0.02),
                'eta': np.arange(0.05, 0.5, 0.05),
            }
            opt_model = XGBClassifier(
                scale_pos_weight=scale_pos_weight,
                enable_categorical=enable_categorical,
                **self._classifier_kwargs
            )
            search = GridSearchCV(estimator=opt_model,
                                  param_grid=params,
                                  scoring='accuracy',
                                  n_jobs=4,
                                  verbose=1, error_score='raise')
            search.fit(x, y)
            classifier_args = {
                k: v
                for k, v
                in itertools.chain(
                    self._classifier_kwargs.items(),
                    search.best_params_.items())
            }
        self.base_model = XGBClassifier(
            scale_pos_weight=scale_pos_weight,
            enable_categorical=enable_categorical,
            tree_method='hist',
            **classifier_args
        )
        self.base_model.fit(
            x,
            y,
            sample_weight=sample_weights,
            **kwargs
        )
        self.optimizer = ThresholdOptimizer(
            estimator=self.base_model,
            constraints=constraints,
            objective=objective,
            predict_method=predict_method,
            grid_size=grid_size,
            flip=flip,
            prefit=True
        )
        self.optimizer.fit(x, y, sensitive_features=x[self.sensitive_feature_names])

    def predict(
            self,
            x: ArrayLike,
            random_state=None
    ):
        try:
            assert self.optimizer is not None
        except AssertionError:
            raise PredictBeforeFitError("You must run fit() before running predict()")
        return self.optimizer.predict(x, sensitive_features=x[self.sensitive_feature_names], random_state=random_state)

    def predict_proba_(
            self,
            x: ArrayLike,
            random_state=None
    ):
        try:
            assert self.optimizer is not None
        except AssertionError:
            raise PredictBeforeFitError("You must run fit() before running predict()")
        return self.optimizer._pmf_predict(x, sensitive_features=x[self.sensitive_feature_names])[:, 1]
        # return self.optimizer(x, sensitive_features=x[self.sensitive_feature_names], random_state=random_state)

    def compute_metrics(self, x: pd.DataFrame, y: pd.Series, use_base_model=False):
        predictions = self.predict(x) if not use_base_model else self.base_model.predict(x)
        precision = precision_score(y, predictions)
        accuracy = accuracy_score(y, predictions)
        tpr = fairlearn.metrics.true_positive_rate(y, predictions)
        tnr = fairlearn.metrics.true_negative_rate(y, predictions)
        fpr = fairlearn.metrics.false_positive_rate(y, predictions)
        fnr = fairlearn.metrics.false_negative_rate(y, predictions)
        total_rows = x.shape[0]
        total_rows_with_any_sensitive_class = sum(
            x[sensitive_classes].any(axis='columns').apply(lambda v: 1 if v else 0)
        )
        equalized_odds_difference_any = fairlearn.metrics.equalized_odds_difference(
            y,
            predictions,
            sensitive_features=x[sensitive_classes]
        )
        equal_odds_ratio_any = fairlearn.metrics.equalized_odds_ratio(
            y,
            predictions,
            sensitive_features=x[sensitive_classes]
        )
        for column in sensitive_classes:
            total = sum(x[column])
            ratio = (total / total_rows)
            equalized_odds_difference_for_class = fairlearn.metrics.equalized_odds_difference(
                y,
                predictions,
                sensitive_features=x[column]
            )


def main(
        input_filename: str,
        binary_outcome_column: str,
        sensitive_classes: list[str],
        sample_weights_col: str = None,
        reference_classes: list[str] = None,
        pos_outcome_indicator: str = '1',
        test_filename: str = None,
        train_test_split_percent: float = 0.8,
        random_state: int = 0,
        use_pos_weights: bool = True,
        enable_categorical: bool = True,
        do_hyperparameter_optimization: bool = False,
        debug: bool = False
):
    input_df = pd.read_csv(input_filename, dtype={binary_outcome_column: str})
    col_translate = {ord('['): '===', ord(']'): '+++', ord('<'): '---'}
    remap_cols = {k: v for k, v in {col: col.translate(col_translate) for col in input_df.columns}.items() if k != v}
    demap_cols = {v: k for k, v in remap_cols.items()}
    binary_outcome_column = remap_cols.get(binary_outcome_column, binary_outcome_column)
    sample_weights_col = remap_cols.get(sample_weights_col, sample_weights_col)
    sensitive_classes = [remap_cols.get(col, col) for col in sensitive_classes]
    reference_classes = [remap_cols.get(col, col) for col in reference_classes]
    input_df = input_df.rename(columns=remap_cols)
    x = input_df.drop(columns=[binary_outcome_column], axis=1)
    if sample_weights_col is not None:
        x = x.drop(columns=[sample_weights_col], axis=1)
    if enable_categorical:
        for col in x.columns:
            if pd.api.types.is_string_dtype(x[col]):
                if len(x[col].unique()) > 2:
                    x[col] = x[col].astype('category')
    y = input_df[binary_outcome_column] == pos_outcome_indicator.strip()
    if test_filename is None:
        x_train, x_test, y_train, y_test = train_test_split(
            x,
            y,
            train_size=train_test_split_percent,
            random_state=random_state
        )
    else:
        x_train = x
        y_train = y
        test_df = pd.read_csv(test_filename, dtype={binary_outcome_column: str}).rename(columns=remap_cols)
        x_test = test_df.drop(columns=[binary_outcome_column])
        y_test = test_df[binary_outcome_column] == pos_outcome_indicator.strip()
    pos_weight = (len(y_train) / y_train.sum().item()) - 1
    fm = FairModel(sensitive_classes)
    fm.fit(
        x_train,
        y_train,
        sample_weights=None if sample_weights_col is None else input_df[sample_weights_col],
        scale_pos_weight=pos_weight if use_pos_weights else None,
        do_hyperparameter_optimization=do_hyperparameter_optimization
    )
    measure_df = pd.concat([x_test, y_test], axis=1)
    measure_df = measure_df.assign(
        fair_predictions=fm.predict_proba_(x_test),
        base_predictions=fm.base_model.predict_proba(x_test)[:, 1]
    )
    base_measure = measure(
        inp=measure_df,
        binary_outcome_column=binary_outcome_column,
        protected_classes=sensitive_classes,
        reference_classes=reference_classes,
        probability_column="base_predictions",
        pos_outcome_indicator="True",
        sample_weights_column=sample_weights_col,
        extra_display_cols=[('model', 'base model')]
    )
    fair_measure = measure(
        inp=measure_df,
        binary_outcome_column=binary_outcome_column,
        protected_classes=sensitive_classes,
        reference_classes=reference_classes,
        probability_column="fair_predictions",
        pos_outcome_indicator="True",
        sample_weights_column=sample_weights_col,
        extra_display_cols=[('model', 'fair model')]
    )
    fm.compute_metrics(x, y)

    for model_name, model in [("base_model", fm.base_model), ("fair_model", fm)]:
        predictions_base = model.predict(x_test)
        precision_base = precision_score(y_test, predictions_base)
        accuracy_base = accuracy_score(y_test, predictions_base)
        tpr_base = fairlearn.metrics.true_positive_rate(y_test, predictions_base)
        tnr_base = fairlearn.metrics.true_negative_rate(y_test, predictions_base)
        fpr_base = fairlearn.metrics.false_positive_rate(y_test, predictions_base)
        fnr_base = fairlearn.metrics.false_negative_rate(y_test, predictions_base)
        if debug:
            print(
                f'{model_name}'
                f' Precision: {precision_base},'
                f' Base model accuracy: {accuracy_base},'
                f' TNR:{tnr_base},'
                f' TPR: {tpr_base},'
                f' FPR: {fpr_base},'
                f' FNR: {fnr_base}'
            )
        for column in sensitive_classes:
            if debug:
                print(
                    f"{model_name} "
                    f"Demographic specific for: {column}"
                )
                print(
                    f"\t"
                    f"{model_name} "
                    f"Total {column} in train: {sum(x_train[column])} "
                    f"or {(sum(x_train[column]) / x_train.shape[0]) * 100}%"
                )
                print(
                    f"\t"
                    f"{model_name} "
                    f"Total {column} in test: {sum(x_test[column])} "
                    f"or {(sum(x_test[column]) / x_test.shape[0]) * 100}%"
                )
            eod_base = fairlearn.metrics.equalized_odds_difference(
                y_test,
                predictions_base,
                sensitive_features=x_test[column]
            )
            if debug:
                print(
                    f"\t"
                    f"{model_name} "
                    f"equal odds difference for group {column}: {eod_base}"
                )
    return fm


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--do-demo", action="store_true",
                        help="Perform demo. Performs optimization on embedded diabetes file.")
    parser.add_argument("--input_filename", type=str, help="Input filename")
    parser.add_argument("--test_filename", type=str, help="Test input filename", default=None)
    parser.add_argument("--sensitive-classes", type=str, help="Comma-separated list of sensitive classes.")
    parser.add_argument("--reference-classes", type=str, help="Comma-separated list of reference classes.")
    parser.add_argument("--binary-outcome-column", type=str, help="Column containing binary outcome data on patient")
    parser.add_argument(
        "--use-pos-weights",
        help="Use autogenerated positive weights. Note: This will ignore the sample-weights if those are specified.",
        action="store_true"
    )
    parser.add_argument(
        "--sample-weights-column",
        help="Sample weights column. Note: specifying use-pos-weights will cause this to be ignored.",
        type=str,
        default=None
    )
    parser.add_argument(
        "--do-hyperparameter-optimization",
        help="Perform hyperparameter optimization",
        action="store_true"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument("--train-test-split", type=float, default=0.8,
                        help="Train/test ratio split of data. Default's to 0.8")
    parser.add_argument("--random-state", type=int, default=0, help="Seed for initializing the random state.")
    args = parser.parse_args()

    if args.do_demo:
        sensitive_classes = [
            'race_AfricanAmerican',
            'race_Asian',
            'race_Hispanic',
            'gender_Female',
        ]
        reference_classes = [
            "gender_Male",
            "race_Caucasian"
        ]
        args.input_filename = os.path.join(os.path.dirname(__file__), "demo/diabetes.csv")
        args.binary_outcome_column = "readmitted"
        args.use_pos_weights = True
    else:
        sensitive_classes = args.sensitive_classes.strip().split(',')
        reference_classes = args.reference_classes.strip().split(',') if args.reference_classes is not None else None

    if args.debug:
        logging.basicConfig(level="DEBUG")

    main(
        input_filename=args.input_filename,
        test_filename=args.test_filename,
        sensitive_classes=sensitive_classes,
        reference_classes=reference_classes,
        binary_outcome_column=args.binary_outcome_column,
        train_test_split_percent=args.train_test_split,
        random_state=args.random_state,
        use_pos_weights=args.use_pos_weights,
        do_hyperparameter_optimization=args.do_hyperparameter_optimization,
        debug=args.debug
    )
