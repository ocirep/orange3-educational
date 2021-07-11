import math

from Orange.evaluation import RMSE, TestOnTrainingData, MAE
from AnyQt.QtCore import Qt, QRectF, QPointF
from AnyQt.QtGui import QColor, QPalette, QPen, QFont

import sklearn.preprocessing as skl_preprocessing
import pyqtgraph as pg
import numpy as np

from orangewidget.report import report
from orangewidget.utils.widgetpreview import WidgetPreview

from Orange.data import Table, Domain
from Orange.data.variable import ContinuousVariable, StringVariable
from Orange.regression.linear import (RidgeRegressionLearner, PolynomialLearner,
                                      LinearRegressionLearner)
from Orange.regression import Learner
from Orange.regression.mean import MeanModel
from Orange.statistics.distribution import Continuous
from Orange.widgets import settings, gui
from Orange.widgets.utils import itemmodels
from Orange.widgets.utils.owlearnerwidget import OWBaseLearner
from Orange.widgets.utils.sql import check_sql_input
from Orange.widgets.widget import Msg, Input, Output


class RegressTo0(Learner):
    @staticmethod
    def fit(*args, **kwargs):
        return MeanModel(Continuous(np.empty(0)))


class OWUnivariateRegression(OWBaseLearner):
    name = "Polynomial Regression"
    description = "Univariate regression with polynomial expansion."
    keywords = ["polynomial regression", "regression",
                "regression visualization", "polynomial features"]
    icon = "icons/UnivariateRegression.svg"
    priority = 500

    class Inputs(OWBaseLearner.Inputs):
        learner = Input("Learner", Learner)

    class Outputs(OWBaseLearner.Outputs):
        coefficients = Output("Coefficients", Table, default=True)
        data = Output("Data", Table)

    replaces = [
        "Orange.widgets.regression.owunivariateregression."
        "OWUnivariateRegression",
        "orangecontrib.prototypes.widgets.owpolynomialregression."
        "OWPolynomialRegression"
    ]

    LEARNER = PolynomialLearner

    learner_name = settings.Setting("Polynomial Regression")

    polynomialexpansion = settings.Setting(1)

    x_var_index = settings.ContextSetting(0)
    y_var_index = settings.ContextSetting(1)
    error_bars_enabled = settings.Setting(False)
    fit_intercept = settings.Setting(True)

    default_learner_name = "Linear Regression"
    error_plot_items = []

    rmse = ""
    mae = ""
    regressor_name = ""

    want_main_area = True
    graph_name = 'plot'

    class Error(OWBaseLearner.Error):
        all_none = Msg("One of the features has no defined values.")
        no_cont_variables = Msg("Polynomial Regression requires at least one numeric feature.")
        same_dep_indepvar = Msg("Dependent and independent variables must be differnt.")

    def add_main_layout(self):

        self.data = None
        self.learner = None

        self.scatterplot_item = None
        self.plot_item = None

        self.x_label = 'x'
        self.y_label = 'y'

        self.rmse = ""
        self.mae = ""
        self.regressor_name = self.default_learner_name

        box = gui.vBox(self.controlArea, "Predictor")
        self.x_var_model = itemmodels.VariableListModel()
        self.comboBoxAttributesX = gui.comboBox(
            box, self, value='x_var_index', callback=self.apply)
        self.comboBoxAttributesX.setModel(self.x_var_model)
        gui.widgetLabel(box, "Polynomial degree")
        self.expansion_spin = gui.hSlider(
            gui.indentedBox(box), self, "polynomialexpansion",
            minValue=0, maxValue=10, ticks=True,
            callback=self.apply)
        gui.checkBox(
            box, self, "fit_intercept",
            label="Fit intercept", callback=self.apply, stateWhenDisabled=True,
            tooltip="Add an intercept term;\n"
                    "This option is always checked if the model is set on input."
        )

        box = gui.vBox(self.controlArea, "Target")
        self.y_var_model = itemmodels.VariableListModel()
        self.comboBoxAttributesY = gui.comboBox(
            box, self, value="y_var_index", callback=self.apply)
        self.comboBoxAttributesY.setModel(self.y_var_model)

        self.error_bars_checkbox = gui.checkBox(
            widget=box, master=self, value='error_bars_enabled',
            label="Show error bars", callback=self.apply)

        gui.rubber(self.controlArea)

        # info box
        info_box = gui.vBox(self.controlArea, "Info")
        self.regressor_label = gui.label(
            widget=info_box, master=self,
            label="Regressor: %(regressor_name).30s")
        gui.label(widget=info_box, master=self,
                  label="Mean absolute error: %(mae).6s")
        gui.label(widget=info_box, master=self,
                  label="Root mean square error: %(rmse).6s")


        # main area GUI
        self.plotview = pg.PlotWidget(background="w")
        self.plot = self.plotview.getPlotItem()

        axis_color = self.palette().color(QPalette.Text)
        axis_pen = QPen(axis_color)

        tickfont = QFont(self.font())
        tickfont.setPixelSize(max(int(tickfont.pixelSize() * 2 // 3), 11))

        axis = self.plot.getAxis("bottom")
        axis.setLabel(self.x_label)
        axis.setPen(axis_pen)
        axis.setTickFont(tickfont)

        axis = self.plot.getAxis("left")
        axis.setLabel(self.y_label)
        axis.setPen(axis_pen)
        axis.setTickFont(tickfont)

        self.plot.setRange(xRange=(0.0, 1.0), yRange=(0.0, 1.0),
                           disableAutoRange=True)

        self.mainArea.layout().addWidget(self.plotview)

    def send_report(self):
        if self.data is None:
            return
        caption = report.render_items_vert((
             ("Polynomial Expansion", self.polynomialexpansion),
             ("Fit intercept",
              self._has_intercept and ["No", "Yes"][self.fit_intercept])
        ))
        self.report_plot()
        if caption:
            self.report_caption(caption)

    def clear(self):
        self.data = None
        self.rmse = ""
        self.mae = ""
        self.clear_plot()

    def clear_plot(self):
        if self.plot_item is not None:
            self.plot_item.setParentItem(None)
            self.plotview.removeItem(self.plot_item)
            self.plot_item = None

        if self.scatterplot_item is not None:
            self.scatterplot_item.setParentItem(None)
            self.plotview.removeItem(self.scatterplot_item)
            self.scatterplot_item = None

        self.remove_error_items()

        self.plotview.clear()

    @check_sql_input
    def set_data(self, data):
        self.clear()
        self.Error.no_cont_variables.clear()
        if data is not None:
            cvars = [var for var in data.domain.variables if var.is_continuous]
            class_cvars = [var for var in data.domain.class_vars
                           if var.is_continuous]

            nvars = len(cvars)
            nclass = len(class_cvars)
            self.x_var_model[:] = cvars
            self.y_var_model[:] = cvars
            if nvars == 0:
                self.data = None
                self.Error.no_cont_variables()
                return

            self.x_var_index = min(max(0, self.x_var_index), nvars - 1)
            if nclass > 0:
                self.y_var_index = min(max(0, nvars-nclass), nvars - 1)
            else:
                self.y_var_index = min(max(0, nvars-1), nvars - 1)
        self.data = data

    @Inputs.learner
    def set_learner(self, learner):
        self.learner = learner
        self.controls.fit_intercept.setDisabled(learner is not None)
        self.regressor_name = (learner.name if learner is not None else self.default_learner_name)

    def handleNewSignals(self):
        self.apply()

    def plot_scatter_points(self, x_data, y_data):
        if self.scatterplot_item:
            self.plotview.removeItem(self.scatterplot_item)
        self.n_points = len(x_data)
        self.scatterplot_item = pg.ScatterPlotItem(
            x=x_data, y=y_data, data=np.arange(self.n_points),
            symbol="o", size=10, pen=pg.mkPen(0.2), brush=pg.mkBrush(0.7),
            antialias=True)
        self.scatterplot_item.opts["useCache"] = False
        self.plotview.addItem(self.scatterplot_item)
        self.plotview.replot()

    def set_range(self, x_data, y_data):
        min_x, max_x = np.nanmin(x_data), np.nanmax(x_data)
        min_y, max_y = np.nanmin(y_data), np.nanmax(y_data)
        if self.polynomialexpansion == 0 and not self._has_intercept:
            if min_y > 0:
                min_y = -0.1 * max_y
            elif max_y < 0:
                max_y = -0.1 * min_y

        self.plotview.setRange(
            QRectF(min_x, min_y, max_x - min_x, max_y - min_y),
            padding=0.025)
        self.plotview.replot()

    def plot_regression_line(self, x_data, y_data):
        item = pg.PlotCurveItem(
            x=x_data, y=y_data,
            pen=pg.mkPen(QColor(255, 0, 0), width=3),
            antialias=True
        )
        self._plot_regression_item(item)

    def plot_infinite_line(self, x, y, angle):
        item = pg.InfiniteLine(
            QPointF(x, y), angle,
            pen=pg.mkPen(QColor(255, 0, 0), width=3))
        self._plot_regression_item(item)

    def _plot_regression_item(self, item):
        if self.plot_item:
            self.plotview.removeItem(self.plot_item)
        self.plot_item = item
        self.plotview.addItem(self.plot_item)
        self.plotview.replot()

    def remove_error_items(self):
        for it in self.error_plot_items:
            self.plotview.removeItem(it)
        self.error_plot_items = []

    def plot_error_bars(self, x,  actual, predicted):
        self.remove_error_items()
        if self.error_bars_enabled:
            for x, a, p in zip(x, actual, predicted):
                line = pg.PlotCurveItem(
                    x=[x, x], y=[a, p],
                    pen=pg.mkPen(QColor(150, 150, 150), width=1),
                    antialias=True)
                self.plotview.addItem(line)
                self.error_plot_items.append(line)
        self.plotview.replot()

    def _varnames(self, name):
        # If variable name is short, use superscripts
        # otherwise "^" because superscripts would be lost
        def ss(x):
            # Compose a (potentially non-single-digit) superscript
            return "".join("⁰¹²³⁴⁵⁶⁷⁸⁹"[i] for i in (int(c) for c in str(x)))

        if len(name) <= 3:
            return [f"{name}{ss(i)}"
                    for i in range(not self._has_intercept,
                                   1 + self.polynomialexpansion)]
        else:
            return ["1"] * self._has_intercept + \
                   [name] * (self.polynomialexpansion >= 1) + \
                   [f"{name}^{i}" for i in range(2, 1 + self.polynomialexpansion)]

    @property
    def _has_intercept(self):
        return self.learner is not None or self.fit_intercept

    def apply(self):
        degree = self.polynomialexpansion
        if degree == 0 and not self.fit_intercept:
            learner = RegressTo0()
        else:
            # For LinearRegressionLearner, set fit_intercept to False:
            # the intercept is added as bias term in polynomial expansion
            # If there is a learner on input, we have not control over this;
            # we include_bias to have the placeholder for the coefficient
            lin_learner = self.learner \
                          or LinearRegressionLearner(fit_intercept=False)
            learner = self.LEARNER(
                preprocessors=self.preprocessors, degree=degree,
                include_bias=self.fit_intercept,
                learner=lin_learner)
        learner.name = self.learner_name
        predictor = None
        model = None

        self.Error.all_none.clear()
        self.Error.same_dep_indepvar.clear()

        if self.data is not None:
            attributes = self.x_var_model[self.x_var_index]
            class_var = self.y_var_model[self.y_var_index]
            if attributes is class_var:
                self.Error.same_dep_indepvar()
                self.clear_plot()
                return

            data_table = Table.from_table(
                Domain([attributes], class_vars=[class_var]), self.data
            )

            # all lines has nan
            if sum(math.isnan(line[0]) or math.isnan(line.get_class())
                   for line in data_table) == len(data_table):
                self.Error.all_none()
                self.clear_plot()
                return

            predictor = learner(data_table)
            model = None
            if hasattr(predictor, "model"):
                model = predictor.model
                if hasattr(model, "model"):
                    model = model.model
                elif hasattr(model, "skl_model"):
                    model = model.skl_model

            preprocessed_data = data_table
            for preprocessor in learner.active_preprocessors:
                preprocessed_data = preprocessor(preprocessed_data)

            x = preprocessed_data.X.ravel()
            y = preprocessed_data.Y.ravel()

            linspace = np.linspace(
                np.nanmin(x), np.nanmax(x), 1000).reshape(-1,1)
            values = predictor(linspace, predictor.Value)

            # calculate prediction for x from data
            validation = TestOnTrainingData()
            predicted = validation(preprocessed_data, [learner])
            self.rmse = round(RMSE(predicted)[0], 6)
            self.mae = round(MAE(predicted)[0], 6)

            # plot error bars
            self.plot_error_bars(
                x, predicted.actual, predicted.predicted.ravel())

            # plot data points
            self.plot_scatter_points(x, y)

            # plot regression line
            x_data, y_data = linspace.ravel(), values.ravel()
            if self.polynomialexpansion == 0:
                self.plot_infinite_line(x_data[0], y_data[0], 0)
            elif self.polynomialexpansion == 1 and hasattr(model, "coef_"):
                k = model.coef_[1 if self._has_intercept else 0]
                self.plot_infinite_line(x_data[0], y_data[0],
                                        math.degrees(math.atan(k)))
            else:
                self.plot_regression_line(x_data, y_data)

            x_label = self.x_var_model[self.x_var_index]
            axis = self.plot.getAxis("bottom")
            axis.setLabel(x_label)

            y_label = self.y_var_model[self.y_var_index]
            axis = self.plot.getAxis("left")
            axis.setLabel(y_label)

            self.set_range(x, y)

        self.Outputs.learner.send(learner)
        self.Outputs.model.send(predictor)

        # Send model coefficents
        if model is not None and hasattr(model, "coef_"):
            domain = Domain([ContinuousVariable("coef")],
                            metas=[StringVariable("name")])
            names = self._varnames(x_label.name)
            coefs = list(model.coef_)
            if self._has_intercept:
                model.coef_[0] += model.intercept_
            coef_table = Table.from_list(domain, list(zip(coefs, names)))
            self.Outputs.coefficients.send(coef_table)
        else:
            self.Outputs.coefficients.send(None)

        self.send_data()

    def send_data(self):
        if self.data is not None:
            attributes = self.x_var_model[self.x_var_index]
            class_var = self.y_var_model[self.y_var_index]

            data_table = Table.from_table(
                Domain([attributes], class_vars=[class_var]), self.data)
            polyfeatures = skl_preprocessing.PolynomialFeatures(
                self.polynomialexpansion, include_bias=self._has_intercept)

            valid_mask = ~np.isnan(data_table.X).any(axis=1)
            if not self._has_intercept and not self.polynomialexpansion:
                x = np.empty((len(data_table), 0))
            else:
                x = data_table.X[valid_mask]
                x = polyfeatures.fit_transform(x)
            x_label = data_table.domain.attributes[0].name

            out_array = np.concatenate((x, data_table.Y[np.newaxis].T[valid_mask]), axis=1)

            out_domain = Domain(
                [ContinuousVariable(name) for name in self._varnames(x_label)],
                class_var)
            self.Outputs.data.send(Table.from_numpy(out_domain, out_array))
            return

        self.Outputs.data.send(None)

    def add_bottom_buttons(self):
        pass

    @classmethod
    def migrate_settings(cls, settings, version):
        # polynomialexpansion used to be controlled by doublespin and was hence
        # float. Just convert to `int`, ignore settings versions.
        settings["polynomialexpansion"] = \
            int(settings.get("polynomialexpansion", 1))


if __name__ == "__main__":
    learner = RidgeRegressionLearner(alpha=1.0)
    iris = Table('iris')
    WidgetPreview(OWUnivariateRegression).run(set_data=iris)#, set_learner=learner)
