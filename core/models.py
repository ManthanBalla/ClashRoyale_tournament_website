from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


class Profile(models.Model):
    PLAN_CHOICES = [
        ('none', 'No Plan'),
        ('1month', '1 Month'),
        ('3month', '3 Months'),
        ('1year', '1 Year'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_creator = models.BooleanField(default=False)
    is_admin = models.BooleanField(default=False)
    upi_id = models.CharField(max_length=100, blank=True, null=True)
    reward_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    creator_plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='none')
    plan_expiry = models.DateTimeField(null=True, blank=True)
    tournaments_created_this_month = models.IntegerField(default=0)
    notify_new_tournaments = models.BooleanField(default=True)
    trophies = models.IntegerField(default=0)
    ingame_username = models.CharField(max_length=100, blank=True, null=True)
    trust_score = models.IntegerField(default=100)

    def is_complete(self):
        return bool(self.upi_id and self.user.first_name and self.user.email and self.ingame_username)

    def plan_active(self):
        from django.utils import timezone
        if self.creator_plan == 'none':
            return False
        if self.plan_expiry and self.plan_expiry > timezone.now():
            return True
        return False

    def tournament_limit(self):
        if self.creator_plan == '1month':
            return 30
        elif self.creator_plan == '3month':
            return 90
        elif self.creator_plan == '1year':
            return 9999
        return 0

    def can_create_tournament(self):
        if self.is_admin:
            return True
        if not self.is_creator:
            return False
        if not self.plan_active():
            return False
        return self.tournaments_created_this_month < self.tournament_limit()

    def __str__(self):
        return self.user.username


class Tournament(models.Model):
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('ongoing', 'Ongoing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    REWARD_TYPE_CHOICES = [
        ('cash', 'Cash'),
        ('gift_card', 'Gift Card'),
        ('ingame', 'In-Game Item'),
        ('other', 'Other'),
    ]

    name = models.CharField(max_length=100)
    description = models.TextField()
    rules = models.TextField(blank=True, null=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    password = models.CharField(max_length=50, blank=True, null=True)
    reward = models.CharField(max_length=100, blank=True)
    reward_type = models.CharField(max_length=20, choices=REWARD_TYPE_CHOICES, default='other')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    join_deadline = models.DateTimeField(null=True, blank=True)
    proof_image = models.ImageField(upload_to='proofs/', blank=True, null=True)
    result_screenshot = models.ImageField(upload_to='results/', blank=True, null=True)
    reward_screenshot = models.ImageField(upload_to='rewards/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_paid = models.BooleanField(default=False)
    entry_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    min_players = models.IntegerField(default=2)
    max_players = models.IntegerField(default=100)
    prize_pool = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    show_participants = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')
    cancel_reason = models.TextField(blank=True, null=True)

    def current_prize_pool(self):
        count = self.participant_set.count()
        return self.entry_fee * count

    def __str__(self):
        return self.name


class Participant(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE)
    joined_at = models.DateTimeField(auto_now_add=True)
    fee_paid = models.BooleanField(default=False)

    class Meta:
        unique_together = ('user', 'tournament')

    def __str__(self):
        return f"{self.user.username} - {self.tournament.name}"


class Match(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE)
    player1 = models.ForeignKey(User, on_delete=models.CASCADE, related_name='player1')
    player2 = models.ForeignKey(User, on_delete=models.CASCADE, related_name='player2')
    winner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='winner')
    round_number = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.player1} vs {self.player2}"


class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    upi_id = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    requested_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - ₹{self.amount} - {self.status}"


class RewardCode(models.Model):
    code = models.CharField(max_length=200)
    description = models.CharField(max_length=200, blank=True)
    tournament = models.ForeignKey('Tournament', on_delete=models.SET_NULL, null=True, blank=True)
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_reward_codes')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} - {'Sent' if self.sent else 'Available'}"


class CreatorMembership(models.Model):
    PLAN_CHOICES = [
        ('1month', '1 Month - 30 Tournaments'),
        ('3month', '3 Months - 90 Tournaments'),
        ('1year', '1 Year - Unlimited Tournaments'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES)
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.username} - {self.plan}"


class Transaction(models.Model):
    TYPE_CHOICES = [
        ('credit', 'Credit'),
        ('debit', 'Debit'),
    ]
    REASON_CHOICES = [
        ('tournament_join', 'Tournament Join'),
        ('tournament_refund', 'Tournament Refund'),
        ('tournament_win', 'Tournament Win'),
        ('creator_share', 'Creator Share'),
        ('admin_share', 'Admin Share'),
        ('membership_purchase', 'Membership Purchase'),
        ('withdrawal', 'Withdrawal'),
        ('withdrawal_refund', 'Withdrawal Refund'),
        ('admin_topup', 'Admin Top Up'),
    ]
    CATEGORY_CHOICES = [
        ('credit', 'Credit'),
        ('debit', 'Debit'),
        ('refund', 'Refund'),
        ('winning', 'Winning'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='credit')
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    tournament = models.ForeignKey('Tournament', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    payment = models.ForeignKey('Payment', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.transaction_type} - ₹{self.amount}"


class Notification(models.Model):
    TYPE_CHOICES = [
        ('tournament_start', 'Tournament Started'),
        ('tournament_end', 'Tournament Ended'),
        ('tournament_cancel', 'Tournament Cancelled'),
        ('result_uploaded', 'Result Uploaded'),
        ('wallet_credit', 'Wallet Credit'),
        ('general', 'General'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    notification_type = models.CharField(max_length=30, choices=TYPE_CHOICES, default='general')
    title = models.CharField(max_length=200)
    message = models.TextField()
    url = models.CharField(max_length=255, blank=True, default='')
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.title}"


class Payment(models.Model):
    STATUS_CHOICES = [
        ('created', 'Created'),
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('expired', 'Expired'),
    ]

    PURPOSE_CHOICES = [
        ('wallet_topup', 'Wallet Top-up'),
        ('creator_membership', 'Creator Membership'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference_id = models.CharField(max_length=20, unique=True, help_text='Unique reference ID e.g. CA1234')
    utr = models.CharField(max_length=30, blank=True, null=True, unique=True, help_text='UPI Transaction Reference')
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, default='wallet_topup')
    plan = models.CharField(max_length=20, blank=True, null=True, help_text='Plan key for membership payments')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    wallet_credited = models.BooleanField(default=False)
    failure_reason = models.CharField(max_length=255, blank=True, null=True)
    raw_payload = models.JSONField(blank=True, null=True)
    verified_via = models.CharField(max_length=20, blank=True, null=True, help_text='sms_auto or user_utr')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - ₹{self.amount} - {self.reference_id} - {self.status}"


class SMSLog(models.Model):
    """Logs every incoming SMS from the webhook for audit and debugging."""
    sender = models.CharField(max_length=100, blank=True)
    message = models.TextField()
    timestamp = models.CharField(max_length=100, blank=True, help_text='Raw timestamp from SMS forwarder')
    parsed_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    parsed_utr = models.CharField(max_length=30, blank=True, null=True)
    matched_payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='sms_logs')
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"SMS from {self.sender} - ₹{self.parsed_amount} - UTR:{self.parsed_utr}"


class DisputeReport(models.Model):
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('resolved', 'Resolved'),
        ('rejected', 'Rejected'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='disputes')
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name='disputes')
    match = models.ForeignKey(Match, on_delete=models.SET_NULL, null=True, blank=True, related_name='disputes')
    message = models.TextField()
    proof_image = models.ImageField(upload_to='disputes/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    admin_note = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} - {self.tournament.name} - {self.status}"


class CreatorFollow(models.Model):
    follower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='creator_follows')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='creator_followers')
    notifications_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('follower', 'creator')

    def __str__(self):
        return f"{self.follower.username} -> {self.creator.username}"


class Cup(models.Model):
    REWARD_TYPE_CHOICES = [
        ('cash', 'Cash'),
        ('gift_card', 'Gift Card'),
    ]
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('ongoing', 'Ongoing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    name = models.CharField(max_length=120)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cups_created')
    reward_type = models.CharField(max_length=20, choices=REWARD_TYPE_CHOICES)
    prize_pool = models.DecimalField(max_digits=10, decimal_places=2)
    rules = models.TextField()
    eligibility_criteria = models.CharField(max_length=255, default='12000+ trophies')
    min_trophies = models.IntegerField(default=12000)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    max_players = models.IntegerField(default=32)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')
    bracket_generated = models.BooleanField(default=False)
    is_bracket_generated = models.BooleanField(default=False)
    bracket_locked = models.BooleanField(default=True)
    shuffled_player_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class CupJoinGuide(models.Model):
    cup = models.OneToOneField(Cup, on_delete=models.CASCADE, related_name='join_guide')
    clan_name = models.CharField(max_length=100)
    clan_tag = models.CharField(max_length=50)
    instructions = models.TextField(help_text='How players should join and play friendly matches.')

    def __str__(self):
        return f"Guide - {self.cup.name}"


class CupParticipant(models.Model):
    cup = models.ForeignKey(Cup, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cup_participations')
    ingame_username = models.CharField(max_length=100, default='')
    trophies_snapshot = models.IntegerField(default=0)
    kicked = models.BooleanField(default=False)
    banned = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('cup', 'user')

    def __str__(self):
        return f"{self.user.username} - {self.cup.name}"


class CupMatch(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('awaiting_confirmation', 'Awaiting Confirmation'),
        ('disputed', 'Disputed'),
        ('completed', 'Completed'),
    ]
    RESULT_SOURCE_CHOICES = [
        ('creator_proof', 'Creator + Proof'),
        ('dual_confirmation', 'Dual Confirmation'),
        ('auto_bye', 'Auto BYE Advance'),
        ('admin_override', 'Admin Override'),
    ]

    cup = models.ForeignKey(Cup, on_delete=models.CASCADE, related_name='cup_matches')
    round_number = models.IntegerField()
    match_number = models.IntegerField()
    player1 = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cup_player1_matches')
    player2 = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cup_player2_matches')
    player1_label = models.CharField(max_length=100, blank=True, default='')
    player2_label = models.CharField(max_length=100, blank=True, default='')
    winner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cup_winner_matches')
    winner_label = models.CharField(max_length=100, blank=True, default='')
    proof_image = models.ImageField(upload_to='cup_results/', null=True, blank=True)
    result_source = models.CharField(max_length=30, choices=RESULT_SOURCE_CHOICES, null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')
    is_locked = models.BooleanField(default=False)
    is_disputed = models.BooleanField(default=False)
    dispute_reason = models.TextField(blank=True, default='')
    deadline = models.DateTimeField(null=True, blank=True)
    next_match = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='previous_matches')
    next_slot = models.IntegerField(null=True, blank=True, help_text='1 for player1 slot, 2 for player2 slot')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('cup', 'round_number', 'match_number')
        ordering = ['round_number', 'match_number']

    def __str__(self):
        return f"{self.cup.name} - R{self.round_number}M{self.match_number}"


class CupMatchConfirmation(models.Model):
    DECISION_CHOICES = [
        ('accept', 'Accept Result'),
        ('dispute', 'Dispute Result'),
    ]
    match = models.ForeignKey(CupMatch, on_delete=models.CASCADE, related_name='confirmations')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    claimed_winner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='claimed_cup_wins')
    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, default='accept')
    dispute_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('match', 'user')


class CupActionLog(models.Model):
    ACTION_CHOICES = [
        ('create_cup', 'Create Cup'),
        ('join_cup', 'Join Cup'),
        ('generate_matches', 'Generate Matches'),
        ('mark_winner', 'Mark Winner'),
        ('dual_confirm', 'Dual Confirm Result'),
        ('player_dispute', 'Player Dispute'),
        ('resolve_dispute', 'Resolve Dispute'),
        ('kick_player', 'Kick Player'),
        ('ban_player', 'Ban Player'),
        ('admin_override', 'Admin Override'),
        ('auto_advance_bye', 'Auto Advance BYE'),
    ]

    cup = models.ForeignKey(Cup, on_delete=models.CASCADE, related_name='action_logs')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cup_actions')
    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES)
    match = models.ForeignKey(CupMatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='action_logs')
    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='cup_target_actions')
    message = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def save_profile(sender, instance, **kwargs):
    try:
        instance.profile.save()
    except Profile.DoesNotExist:
        Profile.objects.create(user=instance)