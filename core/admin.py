from django.contrib import admin
from .models import Tournament, Participant, Profile, Payment, Transaction, DisputeReport

admin.site.register(Tournament)
admin.site.register(Participant)
admin.site.register(Profile)
admin.site.register(Payment)
admin.site.register(Transaction)
admin.site.register(DisputeReport)