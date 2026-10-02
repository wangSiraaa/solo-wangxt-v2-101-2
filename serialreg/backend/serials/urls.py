from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .stocktake_views import StocktakeViewSet
from .views import (
    BindingViewSet, IssueNumberViewSet, IssueViewSet, ItemViewSet,
    TimelineViewSet, TitleViewSet,
)

router = DefaultRouter()
router.register("titles", TitleViewSet)
router.register("numbers", IssueNumberViewSet)
router.register("issues", IssueViewSet)
router.register("items", ItemViewSet, basename="item")
router.register("bindings", BindingViewSet)
router.register("timeline", TimelineViewSet, basename="timeline")
router.register("stocktakes", StocktakeViewSet, basename="stocktake")

urlpatterns = [
    path("", include(router.urls)),
]
