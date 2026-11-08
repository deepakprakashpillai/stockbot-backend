from fastapi import APIRouter

from app.api.v1.algorithms import router as algorithms_router
from app.api.v1.algorithms import versions_router as algorithm_versions_router
from app.api.v1.backtests import router as backtests_router
from app.api.v1.datasets import dataset_upload_sessions_router, dataset_versions_router
from app.api.v1.datasets import router as datasets_router
from app.api.v1.experiments import router as experiments_router
from app.api.v1.library import router as library_router
from app.api.v1.realtime import router as realtime_router
from app.api.v1.system import router as system_router

router = APIRouter(prefix="/api/v1")
router.include_router(system_router)
router.include_router(datasets_router)
router.include_router(dataset_versions_router)
router.include_router(dataset_upload_sessions_router)
router.include_router(library_router)
router.include_router(algorithms_router)
router.include_router(algorithm_versions_router)
router.include_router(backtests_router)
router.include_router(experiments_router)
router.include_router(realtime_router)
