from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from .models import Tournament, Cup

class StaticViewSitemap(Sitemap):
    priority = 0.8
    changefreq = 'daily'

    def items(self):
        return ['home', 'cups', 'creators', 'terms', 'privacy', 'contact', 'help']

    def location(self, item):
        return reverse(item)

class TournamentSitemap(Sitemap):
    priority = 0.9
    changefreq = 'hourly'

    def items(self):
        return Tournament.objects.all().order_by('-id')

    def location(self, obj):
        return reverse('tournament_detail', args=[obj.id])

class CupSitemap(Sitemap):
    priority = 0.9
    changefreq = 'hourly'

    def items(self):
        return Cup.objects.all().order_by('-id')

    def location(self, obj):
        return reverse('cup_detail', args=[obj.id])
