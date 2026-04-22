from django.contrib import admin
from .models import Tournament, Participant, Profile, Payment, SMSLog, Transaction, DisputeReport

admin.site.register(Tournament)
admin.site.register(Participant)
admin.site.register(Profile)
admin.site.register(Transaction)
admin.site.register(DisputeReport)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'reference_id', 'utr', 'purpose', 'status', 'verified_via', 'created_at')
    list_filter = ('status', 'purpose', 'verified_via')
    search_fields = ('user__username', 'reference_id', 'utr')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)


@admin.register(SMSLog)
class SMSLogAdmin(admin.ModelAdmin):
    list_display = ('sender', 'parsed_amount', 'parsed_utr', 'matched_payment', 'source_ip', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('sender', 'message', 'parsed_utr')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)