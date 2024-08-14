from django.http import JsonResponse
from django.urls import path

from .views import scrape

urlpatterns = [
    path(
        'scrape/',
        scrape,
        name='scrape_and_populate'
    ),
]
