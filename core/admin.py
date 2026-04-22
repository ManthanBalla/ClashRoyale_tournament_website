from django.contrib import admin
from .models import Tournament, Participant, Profile, Payment, Transaction, DisputeReport

admin.site.register(Tournament)
admin.site.register(Participant)
admin.site.register(Profile)
admin.site.register(Transaction)
admin.site.register(DisputeReport)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'order_id', 'purpose', 'status', 'wallet_credited', 'created_at')
    list_filter = ('status', 'purpose', 'wallet_credited')
    search_fields = ('user__username', 'order_id', 'payment_session_id')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)