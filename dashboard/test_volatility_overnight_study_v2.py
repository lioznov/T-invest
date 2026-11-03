"""Reuse causal v1 cases against v2 plus the deterministic-forest regression."""
from unittest.mock import patch
from . import test_volatility_overnight_study as legacy
from . import volatility_overnight_study_v2 as study


class VolatilityV2Tests(legacy.VolatilityStudyTests):
    def setUp(self):
        context=patch.object(legacy,'study',study)
        context.start()
        self.addCleanup(context.stop)

    def test_forest_prediction_threads_are_pinned_without_altering_training_parameters(self):
        from sklearn.ensemble import RandomForestRegressor
        train,cal=legacy.examples('2019-01-01',200),legacy.examples('2023-01-01',60)
        model=RandomForestRegressor(n_jobs=2)
        bundle={'estimators':[model]}
        with patch.object(study.base,'fit_variant',return_value=bundle),patch.object(study,'predict',return_value=[.2]*len(cal)):
            result=study.fit_variant(study.VARIANTS[2],train,cal)
        self.assertEqual(model.n_jobs,1)
        self.assertEqual(study.FOREST_PARAMS['n_jobs'],2)
        self.assertEqual(study.base.FOREST_PARAMS['n_jobs'],2)
        self.assertEqual(result['estimators'],[model])
        self.assertGreater(result['interval_abs_pct'],0)
