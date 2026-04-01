from django.contrib import admin
from .models import Tournament, Participant, Profile

admin.site.register(Tournament)
admin.site.register(Participant)
admin.site.register(Profile)