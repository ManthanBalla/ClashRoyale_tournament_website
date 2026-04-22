from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.utils.timezone import localtime
from django.core.cache import cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
import json
import logging
import random
import re
import threading
import hashlib
import hmac
import base64
import requests
from io import BytesIO
import pytz
import string
import uuid
from PIL import Image
from django.contrib.auth.views import PasswordResetConfirmView

from .models import Tournament, Participant, Match, Profile, WithdrawalRequest, RewardCode, CreatorMembership, Transaction, Notification, Payment, DisputeReport, CreatorFollow, Cup, CupJoinGuide, CupParticipant, CupMatch, CupMatchConfirmation, CupActionLog
from .forms import NoReuseSetPasswordForm


logger = logging.getLogger(__name__)

SUBSCRIPTION_PLAN_AMOUNT = {
    '1month': Decimal('499.00'),
    '3month': Decimal('999.00'),
    '1year': Decimal('4000.00'),
}

SUBSCRIPTION_PLAN_DAYS = {
    '1month': 30,
    '3month': 90,
    '1year': 365,
}

MAX_REWARD_CODES_PER_TOURNAMENT = 200


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'auth/password_reset_confirm.html'
    form_class = NoReuseSetPasswordForm


# ─── HELPERS ───────────────────────────────────────────────────────────────

def add_transaction(user, transaction_type, reason, amount, description='', category=None, tournament=None, payment=None):
    if category is None:
        if reason in ('tournament_win',):
            category = 'winning'
        elif reason in ('tournament_refund', 'withdrawal_refund'):
            category = 'refund'
        elif transaction_type == 'debit':
            category = 'debit'
        else:
            category = 'credit'

    Transaction.objects.create(
        user=user,
        transaction_type=transaction_type,
        category=category,
        reason=reason,
        amount=amount,
        tournament=tournament,
        payment=payment,
        description=description
    )


def credit_wallet(user, amount, reason, description='', tournament=None, payment=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Credit amount must be greater than zero.')

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        profile.reward_balance += amount
        profile.save(update_fields=['reward_balance'])
        add_transaction(
            user, 'credit', reason, amount, description,
            tournament=tournament, payment=payment
        )


def debit_wallet(user, amount, reason, description='', tournament=None, payment=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Debit amount must be greater than zero.')

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        if profile.reward_balance < amount:
            raise ValueError('Insufficient wallet balance.')
        profile.reward_balance -= amount
        profile.save(update_fields=['reward_balance'])
        add_transaction(
            user, 'debit', reason, amount, description,
            tournament=tournament, payment=payment
        )


def send_notification(user, notification_type, title, message, tournament=None):
    Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        message=message,
        tournament=tournament
    )


def notify_all_participants(tournament, notification_type, title, message):
    participants = Participant.objects.filter(tournament=tournament)
    for p in participants:
        send_notification(p.user, notification_type, title, message, tournament)
    send_notification(tournament.creator, notification_type, title, message, tournament)


def parse_and_convert(dt_str, tz_choice):
    if not dt_str:
        return None
    try:
        naive = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        if tz_choice == 'IST':
            ist = pytz.timezone('Asia/Kolkata')
            aware = ist.localize(naive)
        else:
            utc = pytz.utc
            aware = utc.localize(naive)
        return aware
    except Exception:
        return dt_str


def sync_tournament_status(tournament, now=None):
    """
    Keep tournament.status aligned with time:
    - before start: upcoming
    - between start and end: ongoing
    - after end: completed
    Cancelled/completed remain final states.
    """
    if tournament.status in ('cancelled', 'completed'):
        return tournament.status

    now = now or timezone.now()

    if tournament.end_time and now >= tournament.end_time:
        expected_status = 'completed'
    elif tournament.start_time and now >= tournament.start_time:
        expected_status = 'ongoing'
    else:
        expected_status = 'upcoming'

    if tournament.status != expected_status:
        tournament.status = expected_status
        tournament.save(update_fields=['status'])

    if tournament.status == 'ongoing':
        maybe_auto_start_tournament(tournament)

    return tournament.status


def maybe_auto_start_tournament(tournament):
    if Match.objects.filter(tournament=tournament).exists():
        return

    participant_ids = list(
        Participant.objects.filter(tournament=tournament).values_list('user_id', flat=True)
    )
    if len(participant_ids) < tournament.min_players:
        if tournament.is_paid:
            for uid in participant_ids:
                user = User.objects.filter(id=uid).first()
                if user:
                    credit_wallet(
                        user,
                        tournament.entry_fee,
                        'tournament_refund',
                        f'♻️ Refund — {tournament.name} cancelled (minimum players not reached)',
                        tournament=tournament
                    )
        tournament.status = 'cancelled'
        tournament.cancel_reason = (
            f'Minimum {tournament.min_players} players required but only '
            f'{len(participant_ids)} joined.'
        )
        tournament.save(update_fields=['status', 'cancel_reason'])
        notify_all_participants(
            tournament,
            'tournament_cancel',
            f'❌ Tournament Cancelled — {tournament.name}',
            f'Not enough players joined. Minimum {tournament.min_players} required.'
        )
        return

    random.shuffle(participant_ids)
    for i in range(0, len(participant_ids), 2):
        if i + 1 < len(participant_ids):
            Match.objects.create(
                tournament=tournament,
                player1_id=participant_ids[i],
                player2_id=participant_ids[i + 1],
                round_number=1
            )

    notify_all_participants(
        tournament,
        'tournament_start',
        f'🔴 {tournament.name} is Now Live!',
        'The tournament has started automatically because start time is live.'
    )


def check_rate_limit(key, limit=20, window_seconds=60):
    current = cache.get(key, 0)
    if current >= limit:
        return False
    if current == 0:
        cache.set(key, 1, timeout=window_seconds)
    else:
        cache.incr(key)
    return True


def is_google_reward_code(code):
    text = f"{code.code} {code.description}".lower()
    return 'google' in text or 'play' in text


def extract_reward_amount(text):
    if not text:
        return Decimal('0.00')
    normalized = str(text).replace(',', '')
    # Prefer explicit currency markers to avoid parsing random code digits.
    patterns = [
        r'₹\s*(\d+(?:\.\d{1,2})?)',
        r'rs\.?\s*(\d+(?:\.\d{1,2})?)',
        r'inr\s*(\d+(?:\.\d{1,2})?)',
    ]
    for pat in patterns:
        m = re.search(pat, normalized, flags=re.IGNORECASE)
        if m:
            try:
                return Decimal(m.group(1))
            except Exception:
                return Decimal('0.00')
    return Decimal('0.00')


def enqueue_task(task_func, *args):
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        # In eager mode on low-CPU/free hosts, run task in daemon thread
        # so request-response is not blocked by network calls.
        threading.Thread(target=lambda: task_func.delay(*args), daemon=True).start()
    else:
        task_func.delay(*args)


def optimize_uploaded_image(uploaded_file, max_dimension=1600, jpeg_quality=82):
    if not uploaded_file or not getattr(uploaded_file, 'content_type', '').startswith('image/'):
        return uploaded_file
    try:
        image = Image.open(uploaded_file)
        if image.mode in ('RGBA', 'P'):
            image = image.convert('RGB')

        width, height = image.size
        longest = max(width, height)
        if longest > max_dimension:
            ratio = max_dimension / float(longest)
            new_size = (int(width * ratio), int(height * ratio))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        output = BytesIO()
        image.save(output, format='JPEG', optimize=True, quality=jpeg_quality)
        output.seek(0)

        base_name = (uploaded_file.name.rsplit('.', 1)[0] if '.' in uploaded_file.name else uploaded_file.name)[:80]
        return InMemoryUploadedFile(
            file=output,
            field_name=getattr(uploaded_file, 'field_name', None),
            name=f"{base_name}.jpg",
            content_type='image/jpeg',
            size=output.getbuffer().nbytes,
            charset=None,
        )
    except Exception:
        return uploaded_file


# ─── AUTH ──────────────────────────────────────────────────────────────────

def home(request):
    tournaments = Tournament.objects.exclude(status='cancelled').select_related('creator').order_by('-created_at')
    joined_tournaments = []
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)

    participant_counts = {
        row['tournament']: row['cnt']
        for row in Participant.objects.filter(tournament__in=tournaments).values('tournament').annotate(cnt=Count('id'))
    }
    tournament_data = []
    for t in tournaments:
        sync_tournament_status(t)
        count = participant_counts.get(t.id, 0)
        tournament_data.append({
            'tournament': t,
            'count': count,
            'prize_pool': t.entry_fee * count if t.is_paid else None
        })

    global_leaderboard = list(User.objects.annotate(
        total_winnings=Sum(
            'transactions__amount',
            filter=Q(transactions__reason='tournament_win', transactions__transaction_type='credit')
        ),
        tournament_wins=Count('winner', distinct=True),
        reward_wins=Count('rewardcode', filter=Q(rewardcode__sent=True), distinct=True)
    ).filter(
        Q(total_winnings__gt=0) | Q(tournament_wins__gt=0) | Q(reward_wins__gt=0)
    ).order_by('-total_winnings', '-tournament_wins', '-reward_wins', 'username')[:50])

    reward_codes = RewardCode.objects.filter(sent=True, assigned_to__isnull=False).only(
        'assigned_to_id', 'description'
    )
    reward_amount_by_user = {}
    for rc in reward_codes:
        amount = extract_reward_amount(rc.description)
        if amount > 0:
            reward_amount_by_user[rc.assigned_to_id] = reward_amount_by_user.get(rc.assigned_to_id, Decimal('0.00')) + amount

    for user in global_leaderboard:
        tx_amount = user.total_winnings or Decimal('0.00')
        reward_amount = reward_amount_by_user.get(user.id, Decimal('0.00'))
        user.display_winnings = tx_amount + reward_amount
        user.display_wins = (user.tournament_wins or 0) + (user.reward_wins or 0)

    global_leaderboard.sort(
        key=lambda u: (u.display_winnings, u.display_wins, -(u.id or 0)),
        reverse=True
    )
    global_leaderboard = global_leaderboard[:20]

    active_creators = User.objects.filter(
        profile__is_creator=True,
        profile__plan_expiry__gt=timezone.now()
    ).select_related('profile').order_by('username')

    followed_creator_ids = set()
    follow_alert_on_ids = set()
    if request.user.is_authenticated:
        follows = CreatorFollow.objects.filter(follower=request.user)
        followed_creator_ids = set(follows.values_list('creator_id', flat=True))
        follow_alert_on_ids = set(
            follows.filter(notifications_enabled=True).values_list('creator_id', flat=True)
        )

    return render(request, 'home.html', {
        'tournament_data': tournament_data,
        'joined_tournaments': joined_tournaments,
        'global_leaderboard': global_leaderboard,
        'active_creators': active_creators,
        'followed_creator_ids': followed_creator_ids,
        'follow_alert_on_ids': follow_alert_on_ids,
        'join_error': request.GET.get('join_error'),
    })


@login_required
def my_tournaments_view(request):
    joined_tournaments = Tournament.objects.filter(
        participant__user=request.user
    ).distinct().select_related('creator').order_by('-created_at')[:30]

    created_tournaments = Tournament.objects.none()
    if request.user.profile.is_creator or request.user.profile.is_admin:
        created_tournaments = Tournament.objects.filter(
            creator=request.user
        ).select_related('creator').order_by('-created_at')[:30]

    return render(request, 'my_tournaments.html', {
        'joined_tournaments': joined_tournaments,
        'created_tournaments': created_tournaments,
    })


def login_view(request):
    if request.method == "POST":
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect('/')
        else:
            return render(request, 'auth/login.html', {'error': 'Invalid credentials'})
    return render(request, 'auth/login.html')


def register_view(request):
    if request.method == "POST":
        username = request.POST['username']
        email = request.POST['email']
        password = request.POST['password']

        if User.objects.filter(username=username).exists():
            return render(request, 'auth/register.html', {
                'error': 'Username already taken. Please choose a different username.'
            })
        if User.objects.filter(email=email).exists():
            return render(request, 'auth/register.html', {
                'error': 'An account with this email already exists.'
            })
        if not email:
            return render(request, 'auth/register.html', {'error': 'Email is required.'})

        User.objects.create_user(username=username, email=email, password=password)
        return redirect('/login/?registered=1')

    return render(request, 'auth/register.html')


def logout_view(request):
    logout(request)
    return redirect('/')


# ─── NOTIFICATIONS ─────────────────────────────────────────────────────────

@login_required
def notifications_view(request):
    notifications = Notification.objects.filter(
        user=request.user
    ).order_by('-created_at')[:50]

    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)

    return render(request, 'notifications.html', {
        'notifications': notifications
    })


@login_required
def mark_notification_read(request, notification_id):
    n = get_object_or_404(Notification, id=notification_id, user=request.user)
    n.is_read = True
    n.save()
    return redirect('/notifications/')


@login_required
def notifications_summary_api(request):
    all_notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = all_notifications.filter(is_read=False).count()
    recent = all_notifications[:8]
    return JsonResponse({
        'ok': True,
        'unread_count': unread,
        'items': [
            {
                'id': n.id,
                'title': n.title,
                'message': n.message[:100],
                'is_read': n.is_read,
                'url': n.url or '/notifications/',
            }
            for n in recent
        ]
    })


# ─── PROFILE ───────────────────────────────────────────────────────────────

@login_required
def profile_view(request):
    profile = request.user.profile
    withdrawal_requests = WithdrawalRequest.objects.filter(
        user=request.user
    ).order_by('-requested_at')
    memberships = CreatorMembership.objects.filter(
        user=request.user
    ).order_by('-started_at')
    transactions = Transaction.objects.filter(
        user=request.user
    ).order_by('-created_at')[:30]

    breakdown = {'credit': Decimal('0.00'), 'debit': Decimal('0.00'), 'refund': Decimal('0.00'), 'winning': Decimal('0.00')}
    all_tx = Transaction.objects.filter(user=request.user)
    for tx in all_tx:
        breakdown[tx.category] = breakdown.get(tx.category, Decimal('0.00')) + tx.amount

    recent_topup = Transaction.objects.filter(
        user=request.user,
        reason='admin_topup'
    ).order_by('-created_at').first()

    if request.method == "POST":
        first_name = request.POST.get('first_name', '').strip()
        email = request.POST.get('email', '').strip()
        upi_id = request.POST.get('upi_id', '').strip()
        ingame_username = request.POST.get('ingame_username', '').strip()
        notify_new_tournaments = request.POST.get('notify_new_tournaments') == 'on'

        if not first_name or not email or not upi_id or not ingame_username:
            return render(request, 'profile.html', {
                'profile': profile,
                'withdrawal_requests': withdrawal_requests,
                'memberships': memberships,
                'transactions': transactions,
                'recent_topup': recent_topup,
                'wallet_breakdown': breakdown,
                'error': 'Full name, real email, UPI ID and in-game username are required to keep your profile complete.'
            })

        if email and email != request.user.email:
            if User.objects.filter(email=email).exclude(id=request.user.id).exists():
                return render(request, 'profile.html', {
                    'profile': profile,
                    'withdrawal_requests': withdrawal_requests,
                    'memberships': memberships,
                    'transactions': transactions,
                    'recent_topup': recent_topup,
                    'wallet_breakdown': breakdown,
                        'error': 'This email is already used by another account.'
                })

        request.user.first_name = first_name
        request.user.email = email
        request.user.save()
        profile.upi_id = upi_id
        profile.ingame_username = ingame_username
        profile.notify_new_tournaments = notify_new_tournaments
        profile.save()

        return render(request, 'profile.html', {
            'profile': profile,
            'withdrawal_requests': withdrawal_requests,
            'memberships': memberships,
            'transactions': transactions,
            'wallet_breakdown': breakdown,
            'success': 'Profile updated successfully!'
        })

    return render(request, 'profile.html', {
        'profile': profile,
        'withdrawal_requests': withdrawal_requests,
        'memberships': memberships,
        'transactions': transactions,
        'wallet_breakdown': breakdown,
    })


# Cashfree API URLs
CASHFREE_BASE_URL = "https://sandbox.cashfree.com/pg" if getattr(settings, 'CASHFREE_ENVIRONMENT', 'SANDBOX') == 'SANDBOX' else "https://api.cashfree.com/pg"

@login_required
@require_POST
def create_cashfree_order(request):
    """Create a Cashfree order and return payment_session_id for the checkout SDK."""
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    amount = payload.get('amount')
    purpose = payload.get('purpose', 'wallet_topup')
    plan = payload.get('plan')

    if purpose == 'creator_membership':
        if plan not in SUBSCRIPTION_PLAN_AMOUNT:
            return JsonResponse({'ok': False, 'error': 'Invalid plan selected.'}, status=400)
        amount_decimal = SUBSCRIPTION_PLAN_AMOUNT[plan]
    else:
        try:
            amount_decimal = Decimal(str(amount))
        except Exception:
            return JsonResponse({'ok': False, 'error': 'Invalid amount.'}, status=400)

    if amount_decimal < Decimal('1.00'):
        return JsonResponse({'ok': False, 'error': 'Minimum amount is ₹1.'}, status=400)

    order_id = f"CA_{request.user.id}_{int(timezone.now().timestamp())}"
    
    url = f"{CASHFREE_BASE_URL}/orders"
    headers = {
        "x-api-version": "2023-08-01",
        "x-client-id": settings.CASHFREE_APP_ID,
        "x-client-secret": settings.CASHFREE_SECRET_KEY,
        "Content-Type": "application/json"
    }
    data = {
        "order_id": order_id,
        "order_amount": float(amount_decimal),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": str(request.user.id),
            "customer_email": request.user.email or "user@clasharena.com",
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": f"https://clash-arena.onrender.com/profile/?order_id={order_id}",
            "notify_url": "https://clash-arena.onrender.com/api/cashfree/webhook/"
        }
    }

    try:
        response = requests.post(url, json=data, headers=headers)
        res_data = response.json()
        if response.status_code != 200:
            logger.error(f"Cashfree Order Error: {res_data}")
            return JsonResponse({'ok': False, 'error': res_data.get('message', 'Cashfree Error')}, status=400)
        
        payment_session_id = res_data.get('payment_session_id')
        
        Payment.objects.create(
            user=request.user,
            amount=amount_decimal,
            order_id=order_id,
            payment_session_id=payment_session_id,
            purpose=purpose,
            plan=plan,
            status='created'
        )
        
        return JsonResponse({
            'ok': True,
            'payment_session_id': payment_session_id,
            'order_id': order_id
        })
    except Exception as e:
        logger.exception("Cashfree Order Creation Failed")
        return JsonResponse({'ok': False, 'error': "Server error while creating payment."}, status=500)

@csrf_exempt
def cashfree_webhook(request):
    """Verify Cashfree webhook signature and fulfill payment automatically."""
    if request.method != 'POST':
        return HttpResponse(status=405)

    signature = request.headers.get('x-webhook-signature')
    timestamp = request.headers.get('x-webhook-timestamp')
    
    if not signature or not timestamp:
        return HttpResponse("Missing signature", status=400)

    raw_body = request.body.decode('utf-8')
    data_to_sign = timestamp + raw_body
    secret = settings.CASHFREE_SECRET_KEY
    computed_signature = base64.b64encode(hmac.new(secret.encode('utf-8'), data_to_sign.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')

    if computed_signature != signature:
        logger.warning("Invalid Cashfree signature received")
        return HttpResponse("Invalid signature", status=400)

    try:
        payload = json.loads(raw_body)
        order_data = payload.get('data', {}).get('order', {})
        payment_data = payload.get('data', {}).get('payment', {})
        
        order_id = order_data.get('order_id')
        payment_status = payment_data.get('payment_status')
        
        if not order_id:
            return HttpResponse("Missing order_id", status=400)

        payment = Payment.objects.filter(order_id=order_id).first()
        if not payment:
            logger.error(f"Payment not found for order_id: {order_id}")
            return HttpResponse("Order not found", status=200)

        if payment_status == 'SUCCESS':
            _fulfill_cashfree_payment(payment, payload)
        elif payment_status in ['FAILED', 'CANCELLED']:
            payment.status = 'failed'
            payment.failure_reason = payment_data.get('payment_message', 'Payment failed')
            payment.raw_payload = payload
            payment.save()

        return HttpResponse("OK", status=200)
    except Exception as e:
        logger.exception("Cashfree Webhook Processing Failed")
        return HttpResponse("Internal Server Error", status=500)

def _fulfill_cashfree_payment(payment, payload):
    """Credit user wallet or activate membership upon successful payment."""
    if payment.status == 'success' or payment.wallet_credited:
        return

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(id=payment.id)
        if payment.status == 'success':
            return

        payment.status = 'success'
        payment.wallet_credited = True
        payment.raw_payload = payload
        payment.save()

        user = payment.user
        if payment.purpose == 'wallet_topup':
            profile = Profile.objects.select_for_update().get(user=user)
            profile.reward_balance += payment.amount
            profile.save()
            
            add_transaction(
                user, 'credit', 'admin_topup', payment.amount,
                f'💳 Wallet top-up via Cashfree — Order: {payment.order_id}',
                category='credit', payment=payment
            )
            send_notification(
                user, 'wallet_credit',
                f'✅ Wallet Top-up Successful — ₹{payment.amount}',
                f'₹{payment.amount} added to your wallet via Cashfree.'
            )
        elif payment.purpose == 'creator_membership':
            _activate_membership_cashfree(payment)

def _activate_membership_cashfree(payment):
    """Activate creator membership after successful payment."""
    user = payment.user
    plan = payment.plan
    if plan and plan in SUBSCRIPTION_PLAN_DAYS:
        profile = Profile.objects.select_for_update().get(user=user)
        now = timezone.now()
        days = SUBSCRIPTION_PLAN_DAYS[plan]
        start_base = profile.plan_expiry if profile.plan_expiry and profile.plan_expiry > now else now
        new_expiry = start_base + timedelta(days=days)

        CreatorMembership.objects.create(
            user=user, plan=plan,
            expires_at=new_expiry, is_active=True,
        )
        
        plan_hierarchy = {'none': 0, '1month': 1, '3month': 2, '1year': 3}
        current_tier = plan_hierarchy.get(profile.creator_plan, 0) if profile.plan_active() else 0
        new_tier = plan_hierarchy.get(plan, 0)
        
        if new_tier > current_tier:
            profile.creator_plan = plan

        profile.is_creator = True
        profile.plan_expiry = new_expiry
        profile.tournaments_created_this_month = 0
        profile.save()
        
        add_transaction(
            user, 'debit', 'membership_purchase',
            payment.amount,
            f'💎 Creator membership (+{days} days) via Cashfree',
            category='debit', payment=payment,
        )
        send_notification(
            user, 'general',
            '✅ Membership Activated',
            f'Your {plan} creator membership is now active via Cashfree.'
        )

@login_required
def check_cashfree_status(request):
    """Simple status check for frontend polling."""
    order_id = request.GET.get('order_id')
    if not order_id:
        return JsonResponse({'ok': False, 'error': 'Order ID required.'}, status=400)
    try:
        payment = Payment.objects.get(order_id=order_id, user=request.user)
        return JsonResponse({
            'ok': True,
            'status': payment.status,
            'amount': str(payment.amount),
            'new_balance': str(request.user.profile.reward_balance) if payment.status == 'success' else None
        })
    except Payment.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Order not found.'}, status=404)





# ─── WITHDRAW ──────────────────────────────────────────────────────────────

@login_required
def withdraw_view(request):
    profile = request.user.profile

    if not profile.is_complete():
        return redirect('/profile/?incomplete=1')

    if request.method == "POST":
        amount = request.POST.get('amount')
        try:
            amount = Decimal(str(amount))
        except Exception:
            return render(request, 'withdraw.html', {
                'error': 'Invalid amount.', 'profile': profile
            })

        if amount <= 0:
            return render(request, 'withdraw.html', {
                'error': 'Amount must be greater than 0.', 'profile': profile
            })

        if amount > profile.reward_balance:
            return render(request, 'withdraw.html', {
                'error': f'Insufficient balance. Your balance is ₹{profile.reward_balance}.',
                'profile': profile
            })

        WithdrawalRequest.objects.create(
            user=request.user,
            amount=amount,
            upi_id=profile.upi_id,
            status='approved'
        )
        try:
            debit_wallet(
                request.user, amount, 'withdrawal',
                f'💸 Auto-Withdrawal of ₹{amount} to {profile.upi_id}'
            )
        except ValueError:
            return render(request, 'withdraw.html', {
                'error': f'Insufficient balance. Your balance is ₹{request.user.profile.reward_balance}.',
                'profile': request.user.profile
            })
        send_notification(
            request.user, 'wallet_credit',
            f'💸 Withdrawal Completed — ₹{amount}',
            f'Your withdrawal of ₹{amount} to {profile.upi_id} has been completed successfully.'
        )
        return redirect('/profile/?withdrawn=1')

    return render(request, 'withdraw.html', {'profile': profile})


# ─── SUBSCRIPTION ──────────────────────────────────────────────────────────

@login_required
def subscription_view(request):
    profile = request.user.profile
    memberships = CreatorMembership.objects.filter(
        user=request.user
    ).order_by('-started_at')
    admin_upi = getattr(settings, 'ADMIN_UPI_ID', 'manthanballa08@okicici')
    return render(request, 'subscription.html', {
        'profile': profile,
        'memberships': memberships,
        'plan_amounts': SUBSCRIPTION_PLAN_AMOUNT,
        'cashfree_env': getattr(settings, 'CASHFREE_ENVIRONMENT', 'SANDBOX'),
    })


# ─── TOURNAMENT ────────────────────────────────────────────────────────────

@login_required
def tournament_rules(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    count = Participant.objects.filter(tournament=tournament).count()
    prize_pool = tournament.entry_fee * count if tournament.is_paid else None

    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'tournament_rules.html', {
        'tournament': tournament,
        'count': count,
        'prize_pool': prize_pool
    })


@login_required
def join_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    sync_tournament_status(tournament)
    now = localtime()

    if not request.user.profile.is_complete():
        return redirect('/profile/?incomplete=1')

    # LOCK: already joined
    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect(f'/tournament/{tournament.id}/')

    # LOCK: not upcoming
    if tournament.status != 'upcoming':
        return redirect('/?join_error=not_open')

    # LOCK: deadline passed
    if tournament.join_deadline and now > localtime(tournament.join_deadline):
        return redirect('/?join_error=deadline')

    # LOCK: max players reached
    current_count = Participant.objects.filter(tournament=tournament).count()
    if current_count >= tournament.max_players:
        return redirect('/?join_error=full')

    # PAID TOURNAMENT
    if tournament.is_paid:
        if request.method == "POST":
            agreed = request.POST.get('agreed')
            if not agreed:
                return redirect(f'/rules/{tournament.id}/')

            profile = request.user.profile
            if profile.reward_balance < tournament.entry_fee:
                return render(request, 'tournament_rules.html', {
                    'tournament': tournament,
                    'count': current_count,
                    'prize_pool': tournament.entry_fee * current_count,
                    'error': f'Insufficient balance. You need ₹{tournament.entry_fee} to join. Your balance is ₹{profile.reward_balance}.'
                })

            # deduct fee
            try:
                debit_wallet(
                    request.user,
                    tournament.entry_fee,
                    'tournament_join',
                    f'🎮 Entry fee for {tournament.name}',
                    tournament=tournament
                )
            except ValueError:
                return render(request, 'tournament_rules.html', {
                    'tournament': tournament,
                    'count': current_count,
                    'prize_pool': tournament.entry_fee * current_count,
                    'error': f'Insufficient balance. You need ₹{tournament.entry_fee} to join. Your balance is ₹{request.user.profile.reward_balance}.'
                })

            # notify deduction
            send_notification(
                request.user,
                'wallet_credit',
                f'💸 ₹{tournament.entry_fee} Deducted — {tournament.name}',
                f'₹{tournament.entry_fee} entry fee deducted for joining {tournament.name}. New balance: ₹{request.user.profile.reward_balance}',
                tournament
            )

            tournament.prize_pool += tournament.entry_fee
            tournament.save()

            Participant.objects.create(
                user=request.user,
                tournament=tournament,
                fee_paid=True
            )
            send_notification(
                request.user, 'general',
                f'✅ Joined {tournament.name}',
                f'You have successfully joined {tournament.name}.'
            )
            return redirect(f'/tournament/{tournament.id}/')

        return redirect(f'/rules/{tournament.id}/')

    # FREE TOURNAMENT
    # FREE TOURNAMENT — password reveal
    show_password = False
    if tournament.start_time:
        now_utc = timezone.now()
        if now_utc >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    if tournament.password:
        if request.method == "POST":
            entered_password = request.POST.get('password')
            if entered_password == tournament.password:
                Participant.objects.create(user=request.user, tournament=tournament)
                send_notification(
                    request.user, 'general',
                    f'✅ Joined {tournament.name}',
                    f'You have successfully joined {tournament.name}.'
                )
                return redirect(f'/tournament/{tournament.id}/')
            else:
                return render(request, 'enter_password.html', {
                    'tournament': tournament,
                    'error': 'Wrong password ❌',
                    'show_password': show_password
                })
        return render(request, 'enter_password.html', {
            'tournament': tournament,
            'show_password': show_password
        })

    Participant.objects.create(user=request.user, tournament=tournament)
    send_notification(
        request.user, 'general',
        f'✅ Joined {tournament.name}',
        f'You have successfully joined {tournament.name}.'
    )
    return redirect(f'/tournament/{tournament.id}/')


@login_required
def create_tournament(request):
    profile = request.user.profile

    # ADMIN: unlimited access, no restrictions
    if not profile.is_admin and not profile.is_creator:
        return redirect('/')

    # CREATOR: check plan limits only
    if not profile.is_admin and not profile.can_create_tournament():
        return render(request, 'create_tournament.html', {
            'error': 'You have reached your tournament limit or your plan has expired. Please upgrade your plan.'
        })

    if request.method == "POST":
        name = request.POST['name']
        description = request.POST['description']
        rules = request.POST.get('rules', '')
        password = request.POST.get('password') or None
        reward = request.POST.get('reward', '')
        reward_type = request.POST.get('reward_type', 'other')
        start_time_raw = request.POST.get('start_time')
        end_time_raw = request.POST.get('end_time') or None
        join_deadline_raw = request.POST.get('join_deadline') or None
        proof_image = optimize_uploaded_image(request.FILES.get('proof_image'))
        is_paid = request.POST.get('is_paid') == 'paid'
        entry_fee = request.POST.get('entry_fee', 0) or 0
        min_players = request.POST.get('min_players', 2) or 2
        max_players = request.POST.get('max_players', 100) or 100
        show_participants = request.POST.get('show_participants') == 'on'
        timezone_choice = request.POST.get('timezone_choice', 'IST')

        start_time = parse_and_convert(start_time_raw, timezone_choice)
        end_time = parse_and_convert(end_time_raw, timezone_choice)
        join_deadline = parse_and_convert(join_deadline_raw, timezone_choice)

        try:
            tournament = Tournament.objects.create(
                name=name,
                description=description,
                rules=rules,
                password=password,
                reward=reward,
                reward_type=reward_type,
                start_time=start_time,
                end_time=end_time,
                join_deadline=join_deadline,
                proof_image=proof_image,
                creator=request.user,
                is_paid=is_paid,
                entry_fee=entry_fee,
                min_players=min_players,
                max_players=max_players,
                show_participants=show_participants,
            )
        except Exception:
            logger.exception("Tournament creation failed user_id=%s", request.user.id)
            return render(request, 'create_tournament.html', {
                'error': 'Could not create tournament right now. Please try again.'
            })

        # only increment for non-admin creators
        if not profile.is_admin:
            profile.tournaments_created_this_month += 1
            profile.save()

        if check_rate_limit(f"creator_new_tournament_notify:{request.user.id}", limit=20, window_seconds=3600):
            try:
                from .tasks import notify_creator_followers_task
                enqueue_task(notify_creator_followers_task, request.user.id, tournament.id)
            except Exception:
                logger.exception("Failed to enqueue follower notifications creator_id=%s", request.user.id)

        return redirect('/')

    return render(request, 'create_tournament.html')


@login_required
def delete_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        if tournament.is_paid:
            participants = Participant.objects.filter(tournament=tournament, fee_paid=True)
            for p in participants:
                credit_wallet(
                    p.user,
                    tournament.entry_fee,
                    'tournament_refund',
                    f'♻️ Refund — {tournament.name} deleted by organizer',
                    tournament=tournament
                )
                send_notification(
                    p.user, 'tournament_cancel',
                    f'♻️ Refund — {tournament.name} Deleted',
                    f'Tournament {tournament.name} was deleted. ₹{tournament.entry_fee} refunded to your wallet.',
                    tournament
                )
        tournament.delete()
        return redirect('/')

    return redirect('/')


@login_required
def edit_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        timezone_choice = request.POST.get('timezone_choice', 'IST')

        tournament.name = request.POST['name']
        tournament.description = request.POST['description']
        tournament.rules = request.POST.get('rules', '')
        tournament.password = request.POST.get('password') or None
        tournament.reward = request.POST.get('reward', '')
        tournament.reward_type = request.POST.get('reward_type', 'other')
        tournament.start_time = parse_and_convert(request.POST.get('start_time'), timezone_choice)
        tournament.end_time = parse_and_convert(request.POST.get('end_time') or None, timezone_choice)
        tournament.join_deadline = parse_and_convert(request.POST.get('join_deadline') or None, timezone_choice)
        tournament.min_players = request.POST.get('min_players', 2) or 2
        tournament.max_players = request.POST.get('max_players', 100) or 100
        tournament.show_participants = request.POST.get('show_participants') == 'on'

        if request.FILES.get('proof_image'):
            tournament.proof_image = optimize_uploaded_image(request.FILES.get('proof_image'))

        tournament.save()
        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'edit_tournament.html', {'tournament': tournament})


def tournament_detail(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    sync_tournament_status(tournament)
    participants = Participant.objects.filter(tournament=tournament).select_related('user')
    matches = Match.objects.filter(tournament=tournament).select_related('player1', 'player2', 'winner')
    now = localtime()
    count = participants.count()

    show_password = False
    if tournament.start_time:
        now_utc = timezone.now()
        if now_utc >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    joined_tournaments = []
    is_participant = False
    can_manage_rewards = False
    reward_codes = RewardCode.objects.none()
    reward_recipients = []
    creator_tournaments = Tournament.objects.none()
    likely_winner = None
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)
        is_participant = Participant.objects.filter(
            user=request.user, tournament=tournament
        ).exists()
        can_manage_rewards = request.user.profile.is_admin or request.user == tournament.creator
        if can_manage_rewards:
            reward_codes = RewardCode.objects.filter(sent=False).order_by('-created_at')[:50]
            reward_recipients = User.objects.filter(
                participant__tournament=tournament
            ).distinct().order_by('username')
            creator_tournaments = Tournament.objects.filter(creator=tournament.creator).order_by('-created_at')

    winner_tx = Transaction.objects.filter(
        tournament=tournament,
        reason='tournament_win',
        transaction_type='credit'
    ).select_related('user').order_by('-created_at').first()
    if winner_tx:
        likely_winner = winner_tx.user

    prize_pool = tournament.entry_fee * count if tournament.is_paid else None

    return render(request, 'tournament_detail.html', {
        'tournament': tournament,
        'participants': participants,
        'matches': matches,
        'match_options': matches,
        'open_disputes': DisputeReport.objects.filter(tournament=tournament, status='open').order_by('-created_at')[:10],
        'can_manage_rewards': can_manage_rewards,
        'reward_codes': reward_codes,
        'reward_recipients': reward_recipients,
        'creator_tournaments': creator_tournaments,
        'likely_winner': likely_winner,
        'show_password': show_password,
        'joined_tournaments': joined_tournaments,
        'count': count,
        'prize_pool': prize_pool,
        'is_participant': is_participant,
    })


@login_required
@require_POST
def submit_dispute(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    message = (request.POST.get('message') or '').strip()
    match_id = request.POST.get('match_id')
    proof_image = optimize_uploaded_image(request.FILES.get('proof_image'))

    if not message:
        return redirect(f'/tournament/{tournament.id}/?dispute_error=1')

    match = None
    if match_id:
        match = Match.objects.filter(id=match_id, tournament=tournament).first()

    DisputeReport.objects.create(
        user=request.user,
        tournament=tournament,
        match=match,
        message=message,
        proof_image=proof_image,
    )
    send_notification(
        request.user, 'general',
        '📨 Dispute Submitted',
        f'Your dispute for {tournament.name} has been submitted and will be reviewed by admin.'
    )
    return redirect(f'/tournament/{tournament.id}/?dispute_submitted=1')


@login_required
def upload_results(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        if request.FILES.get('result_screenshot'):
            tournament.result_screenshot = optimize_uploaded_image(request.FILES.get('result_screenshot'))
        if request.FILES.get('reward_screenshot'):
            tournament.reward_screenshot = optimize_uploaded_image(request.FILES.get('reward_screenshot'))
        tournament.status = 'completed'
        tournament.save()

        notify_all_participants(
            tournament,
            'result_uploaded',
            f'📸 Results Uploaded — {tournament.name}',
            f'The organizer has uploaded results for {tournament.name}! Check the leaderboard and reward proof now.'
        )

        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'upload_results.html', {'tournament': tournament})


@login_required
def cancel_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        reason = request.POST.get('cancel_reason', 'Cancelled by organizer')

        if tournament.is_paid:
            participants = Participant.objects.filter(tournament=tournament, fee_paid=True)
            for p in participants:
                credit_wallet(
                    p.user,
                    tournament.entry_fee,
                    'tournament_refund',
                    f'♻️ Refund — {tournament.name} cancelled: {reason}',
                    tournament=tournament
                )

        notify_all_participants(
            tournament,
            'tournament_cancel',
            f'❌ Tournament Cancelled — {tournament.name}',
            f'Reason: {reason}' +
            (f'\n♻️ ₹{tournament.entry_fee} has been refunded to your wallet.' if tournament.is_paid else '')
        )

        tournament.status = 'cancelled'
        tournament.cancel_reason = reason
        tournament.save()
        return redirect('/')

    return redirect('/')


@login_required
def generate_matches(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    maybe_auto_start_tournament(tournament)
    return redirect(f'/tournament/{tournament.id}/')


@login_required
def submit_result(request, match_id):
    match = get_object_or_404(Match, id=match_id)

    if request.user != match.tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        winner_id = request.POST.get('winner')
        winner = User.objects.get(id=winner_id)
        match.winner = winner
        match.save()

        tournament = match.tournament
        all_matches = Match.objects.filter(tournament=tournament)

        if all_matches.count() > 0 and all(m.winner for m in all_matches):
            if tournament.is_paid and tournament.status == 'ongoing':
                from collections import Counter
                wins = Counter(m.winner_id for m in all_matches if m.winner)
                top_winner_id = wins.most_common(1)[0][0]
                top_winner = User.objects.get(id=top_winner_id)

                total_players = Participant.objects.filter(tournament=tournament).count()
                total_collection = tournament.entry_fee * total_players
                prize_pool = (total_collection * Decimal('0.70')).quantize(Decimal('0.01'))
                remaining = total_collection - prize_pool
                creator_share = (remaining * Decimal('0.60')).quantize(Decimal('0.01'))
                admin_share = remaining - creator_share

                # pay winner
                credit_wallet(
                    top_winner, prize_pool,
                    'tournament_win',
                    f'🏆 Prize — Won {tournament.name}',
                    tournament=tournament
                )
                send_notification(
                    top_winner, 'wallet_credit',
                    f'🏆 You Won! ₹{prize_pool} Added',
                    f'Congratulations! You won {tournament.name}. ₹{prize_pool} has been added to your wallet.',
                    tournament
                )

                # pay creator
                credit_wallet(
                    tournament.creator, creator_share,
                    'creator_share',
                    f'🎮 Creator earnings — {tournament.name}',
                    tournament=tournament
                )
                send_notification(
                    tournament.creator, 'wallet_credit',
                    f'💰 Creator Earnings — ₹{creator_share}',
                    f'₹{creator_share} creator share added from {tournament.name}.',
                    tournament
                )

                # pay admin
                admin = User.objects.filter(profile__is_admin=True).first()
                if admin:
                    credit_wallet(
                        admin, admin_share,
                        'admin_share',
                        f'⚙️ Platform fee — {tournament.name}',
                        tournament=tournament
                    )

                tournament.status = 'completed'
                tournament.save()

                notify_all_participants(
                    tournament,
                    'tournament_end',
                    f'🏆 {tournament.name} Has Ended!',
                    f'{tournament.name} has ended! Winner: {top_winner.username}. Check the results now.'
                )

    return redirect(f'/tournament/{match.tournament.id}/')


# ─── CUPS ──────────────────────────────────────────────────────────────────

def is_elite_user(user):
    if not user.is_authenticated:
        return False
    if user.profile.is_admin:
        return True
    return user.profile.creator_plan == '1year' and user.profile.plan_active()


def _sync_cup_status(cup):
    if cup.status in ('cancelled', 'completed'):
        return
    now = timezone.now()
    if cup.end_time and now >= cup.end_time:
        cup.status = 'completed'
    elif cup.start_time and now >= cup.start_time:
        cup.status = 'ongoing'
    else:
        cup.status = 'upcoming'
    cup.save(update_fields=['status'])


def _next_power_of_two(n):
    power = 1
    while power < max(1, n):
        power *= 2
    return power


def _log_cup_action(cup, actor, action_type, message='', match=None, target_user=None, metadata=None):
    CupActionLog.objects.create(
        cup=cup,
        actor=actor,
        action_type=action_type,
        match=match,
        target_user=target_user,
        message=message,
        metadata=metadata or {}
    )


def _notify_cup_users(user_ids, message, url='/cups/'):
    unique_ids = {uid for uid in user_ids if uid}
    if not unique_ids:
        return
    users = User.objects.filter(id__in=unique_ids)
    for user in users:
        Notification.objects.create(
            user=user,
            notification_type='general',
            title='Cup Update',
            message=message,
            url=url
        )


def _get_host_badge(trust_score):
    if trust_score >= 80:
        return 'trusted'
    if trust_score >= 50:
        return 'average'
    return 'risky'


def _advance_cup_winner(match, actor=None):
    if not match.next_match:
        match.cup.status = 'completed'
        match.cup.save(update_fields=['status'])
        if not match.cup.action_logs.filter(action_type='player_dispute').exists():
            creator_profile = match.cup.creator.profile
            creator_profile.trust_score = min(100, creator_profile.trust_score + 3)
            creator_profile.save(update_fields=['trust_score'])
        return
    nxt = match.next_match
    if match.next_slot == 1:
        nxt.player1 = match.winner
        nxt.player1_label = match.winner_label
    else:
        nxt.player2 = match.winner
        nxt.player2_label = match.winner_label
    nxt.save(update_fields=['player1', 'player2', 'player1_label', 'player2_label'])
    if nxt.player1_id and nxt.player2_id:
        _notify_cup_users(
            [nxt.player1_id, nxt.player2_id],
            f'Your next round match is ready in {nxt.cup.name} (Round {nxt.round_number}).',
            url=f'/cups/{nxt.cup.id}/'
        )
    _auto_advance_bye_in_match(nxt, actor=actor)


def _resolve_match_winner(match, winner_user=None, winner_label='', source='creator_proof', proof_image=None, actor=None, completed=False):
    if match.is_locked:
        return
    match.winner = winner_user
    match.winner_label = winner_label or (winner_user.username if winner_user else '')
    match.result_source = source
    if proof_image:
        match.proof_image = optimize_uploaded_image(proof_image)
    if completed:
        match.status = 'completed'
        match.is_locked = True
        match.is_disputed = False
        match.dispute_reason = ''
    else:
        match.status = 'awaiting_confirmation'
    match.save()
    _log_cup_action(
        match.cup,
        actor,
        'mark_winner' if source != 'auto_bye' else 'auto_advance_bye',
        message=f"Winner set for R{match.round_number}M{match.match_number}: {match.winner_label}",
        match=match,
        target_user=winner_user,
        metadata={'source': source}
    )

    if completed:
        _advance_cup_winner(match, actor=actor)


def _auto_advance_bye_in_match(match, actor=None):
    if match.winner or match.winner_label or match.is_locked:
        return
    p1_label = match.player1_label or (match.player1.username if match.player1 else '')
    p2_label = match.player2_label or (match.player2.username if match.player2 else '')
    if p1_label and p2_label:
        if p1_label == 'BYE' and p2_label != 'BYE':
            _resolve_match_winner(match, winner_user=match.player2, winner_label=p2_label, source='auto_bye', actor=actor, completed=True)
        elif p2_label == 'BYE' and p1_label != 'BYE':
            _resolve_match_winner(match, winner_user=match.player1, winner_label=p1_label, source='auto_bye', actor=actor, completed=True)


def _status_badge_class(status):
    mapping = {
        'pending': 'bg-gray-500/20 text-gray-300 border border-gray-500/30',
        'awaiting_confirmation': 'bg-yellow-500/20 text-yellow-300 border border-yellow-500/30',
        'disputed': 'bg-red-500/20 text-red-300 border border-red-500/30',
        'completed': 'bg-green-500/20 text-green-300 border border-green-500/30',
    }
    return mapping.get(status, mapping['pending'])


@login_required
def cups_view(request):
    cups = Cup.objects.select_related('creator').order_by('-created_at')
    cup_data = []
    for cup in cups:
        _sync_cup_status(cup)
        current = cup.participants.filter(kicked=False, banned=False).count()
        cup_data.append({'cup': cup, 'count': current})
    return render(request, 'cups.html', {
        'cup_data': cup_data,
        'is_elite': is_elite_user(request.user),
    })


@login_required
def create_cup(request):
    if not is_elite_user(request.user):
        return render(request, 'create_cup.html', {
            'elite_required': True,
            'error': 'Only Elite (₹4000/year) users can organize their own cup.',
        })

    if request.method == 'POST':
        timezone_choice = request.POST.get('timezone_choice', 'IST')
        start_time = parse_and_convert(request.POST.get('start_time'), timezone_choice)
        end_time = parse_and_convert(request.POST.get('end_time'), timezone_choice)
        cup = Cup.objects.create(
            name=request.POST.get('name', '').strip(),
            creator=request.user,
            reward_type=request.POST.get('reward_type', 'cash'),
            prize_pool=request.POST.get('prize_pool') or 0,
            rules=request.POST.get('rules', '').strip(),
            eligibility_criteria=request.POST.get('eligibility_criteria', '12000+ trophies').strip() or '12000+ trophies',
            min_trophies=int(request.POST.get('min_trophies') or 12000),
            start_time=start_time,
            end_time=end_time,
            max_players=int(request.POST.get('max_players') or 32),
        )
        CupJoinGuide.objects.create(
            cup=cup,
            clan_name=request.POST.get('clan_name', '').strip(),
            clan_tag=request.POST.get('clan_tag', '').strip(),
            instructions=request.POST.get('instructions', '').strip(),
        )
        _log_cup_action(cup, request.user, 'create_cup', message='Cup created by organizer.')
        return redirect(f'/cups/{cup.id}/')

    return render(request, 'create_cup.html', {'elite_required': False})


@login_required
def cup_detail(request, cup_id):
    cup = get_object_or_404(Cup, id=cup_id)
    _sync_cup_status(cup)
    participants = cup.participants.select_related('user').order_by('joined_at')
    count = participants.filter(kicked=False, banned=False).count()
    can_manage = request.user == cup.creator or request.user.profile.is_admin
    is_joined = participants.filter(user=request.user).exists()
    bracket_visible = can_manage or timezone.now() >= cup.start_time
    matches = cup.cup_matches.select_related('player1', 'player2', 'winner').all()
    history = cup.action_logs.filter(action_type__in=['mark_winner', 'auto_advance_bye', 'dual_confirm', 'player_dispute', 'resolve_dispute']).order_by('created_at')
    dispute_matches = matches.filter(status='disputed')
    total_matches = matches.count()
    completed_matches = matches.filter(status='completed').count()
    pending_matches = matches.filter(status='pending').count()
    awaiting_matches = matches.filter(status='awaiting_confirmation').count()
    disputed_matches_count = dispute_matches.count()
    rounds = sorted(set(matches.values_list('round_number', flat=True)))
    round_cards = [{'round': r, 'matches': list(matches.filter(round_number=r))} for r in rounds]
    creator_profile = cup.creator.profile
    creator_total_cups = Cup.objects.filter(creator=cup.creator).count()
    creator_total_disputes = CupMatch.objects.filter(cup__creator=cup.creator, status='disputed').count()
    return render(request, 'cup_detail.html', {
        'cup': cup,
        'participants': participants,
        'count': count,
        'can_manage': can_manage,
        'is_joined': is_joined,
        'bracket_visible': bracket_visible,
        'matches': matches,
        'history': history,
        'dispute_matches': dispute_matches,
        'status_badges': {m.id: _status_badge_class(m.status) for m in matches},
        'total_matches': total_matches,
        'completed_matches': completed_matches,
        'pending_matches': pending_matches,
        'awaiting_matches': awaiting_matches,
        'disputed_matches_count': disputed_matches_count,
        'completion_pct': int((completed_matches * 100) / total_matches) if total_matches else 0,
        'round_cards': round_cards,
        'creator_trust_score': creator_profile.trust_score,
        'creator_host_badge': _get_host_badge(creator_profile.trust_score),
        'creator_total_cups': creator_total_cups,
        'creator_total_disputes': creator_total_disputes,
    })


@login_required
def cup_dispute_queue(request, cup_id):
    cup = get_object_or_404(Cup, id=cup_id)
    if request.user != cup.creator and not request.user.profile.is_admin:
        return redirect('/')
    disputed_matches = cup.cup_matches.select_related('player1', 'player2', 'winner').filter(status='disputed').order_by('round_number', 'match_number')
    return render(request, 'cup_disputes.html', {
        'cup': cup,
        'disputed_matches': disputed_matches,
    })


@login_required
@require_POST
def join_cup(request, cup_id):
    cup = get_object_or_404(Cup, id=cup_id)
    _sync_cup_status(cup)
    if cup.status != 'upcoming' or cup.is_bracket_generated or cup.bracket_generated:
        return redirect(f'/cups/{cup.id}/')
    if cup.participants.filter(user=request.user).exists():
        return redirect(f'/cups/{cup.id}/')
    active_count = cup.participants.filter(kicked=False, banned=False).count()
    if active_count >= cup.max_players:
        return redirect(f'/cups/{cup.id}/?error=full')
    trophies = request.user.profile.trophies
    if not request.user.profile.ingame_username:
        return redirect('/profile/?incomplete=1')
    if trophies < cup.min_trophies and request.POST.get('risk_ack') != 'on':
        return redirect(f'/cups/{cup.id}/?error=risk_ack')

    CupParticipant.objects.create(
        cup=cup,
        user=request.user,
        ingame_username=request.user.profile.ingame_username,
        trophies_snapshot=trophies
    )
    _log_cup_action(cup, request.user, 'join_cup', message=f'{request.user.username} joined the cup.')
    return redirect(f'/cups/{cup.id}/')


@login_required
@require_POST
def generate_cup_matches(request, cup_id):
    cup = get_object_or_404(Cup, id=cup_id)
    if request.user != cup.creator and not request.user.profile.is_admin:
        return redirect('/')
    if cup.bracket_generated or cup.is_bracket_generated:
        return redirect(f'/cups/{cup.id}/?error=already_generated')

    participants = list(
        cup.participants.filter(kicked=False, banned=False).select_related('user').order_by('joined_at')
    )
    shuffled_ids = [p.user_id for p in participants]
    random.shuffle(shuffled_ids)
    bracket_size = _next_power_of_two(len(shuffled_ids))
    while len(shuffled_ids) < bracket_size:
        shuffled_ids.append(None)

    cup.shuffled_player_ids = shuffled_ids
    cup.bracket_generated = True
    cup.is_bracket_generated = True
    cup.save(update_fields=['shuffled_player_ids', 'bracket_generated', 'is_bracket_generated'])

    rounds = bracket_size.bit_length() - 1
    round_matches = {}
    for round_no in range(1, rounds + 1):
        match_count = 2 ** (rounds - round_no)
        round_matches[round_no] = []
        for match_no in range(1, match_count + 1):
            m = CupMatch.objects.create(cup=cup, round_number=round_no, match_number=match_no)
            round_matches[round_no].append(m)

    for round_no in range(1, rounds):
        for idx, match in enumerate(round_matches[round_no]):
            nxt = round_matches[round_no + 1][idx // 2]
            match.next_match = nxt
            match.next_slot = 1 if idx % 2 == 0 else 2
            match.save(update_fields=['next_match', 'next_slot'])

    first_round = round_matches[1]
    for idx, match in enumerate(first_round):
        p1_id = shuffled_ids[idx * 2]
        p2_id = shuffled_ids[idx * 2 + 1]
        if p1_id:
            u1 = User.objects.get(id=p1_id)
            match.player1 = u1
            match.player1_label = u1.username
        else:
            match.player1 = None
            match.player1_label = 'BYE'
        if p2_id:
            u2 = User.objects.get(id=p2_id)
            match.player2 = u2
            match.player2_label = u2.username
        else:
            match.player2 = None
            match.player2_label = 'BYE'
        match.save(update_fields=['player1', 'player2', 'player1_label', 'player2_label'])
        _notify_cup_users(
            [match.player1_id, match.player2_id],
            f'Match assigned in {cup.name}: Round {match.round_number}, Match {match.match_number}.',
            url=f'/cups/{cup.id}/'
        )
        _auto_advance_bye_in_match(match, actor=request.user)

    _log_cup_action(cup, request.user, 'generate_matches', message='Bracket generated and locked.')
    return redirect(f'/cups/{cup.id}/')


@login_required
@require_POST
def mark_cup_winner(request, match_id):
    winner_id = request.POST.get('winner_id')
    proof_image = request.FILES.get('proof_image')
    if not winner_id or not proof_image:
        match = get_object_or_404(CupMatch, id=match_id)
        cup = match.cup
        return redirect(f'/cups/{cup.id}/?error=proof_required')

    with transaction.atomic():
        match = CupMatch.objects.select_for_update().select_related('cup', 'player1', 'player2').get(id=match_id)
        cup = match.cup
        if request.user != cup.creator and not request.user.profile.is_admin:
            return redirect('/')
        if match.is_locked or match.status == 'completed':
            return redirect(f'/cups/{cup.id}/?error=locked')
        winner = User.objects.filter(id=winner_id).first()
        if winner not in [match.player1, match.player2]:
            return redirect(f'/cups/{cup.id}/?error=invalid_winner')
        _resolve_match_winner(
            match,
            winner_user=winner,
            winner_label=winner.username,
            source='creator_proof',
            proof_image=proof_image,
            actor=request.user,
            completed=False
        )
        _notify_cup_users(
            [match.player1_id, match.player2_id],
            f'Result submitted for your match in {cup.name}. Please accept or dispute.',
            url=f'/cups/{cup.id}/'
        )
    return redirect(f'/cups/{cup.id}/')


@login_required
@require_POST
def confirm_cup_match_result(request, match_id):
    with transaction.atomic():
        match = CupMatch.objects.select_for_update().select_related('cup', 'player1', 'player2', 'winner').get(id=match_id)
        cup = match.cup
        if request.user not in [match.player1, match.player2]:
            return redirect(f'/cups/{cup.id}/')
        if match.status != 'awaiting_confirmation' or not match.winner:
            return redirect(f'/cups/{cup.id}/?error=status_changed')
        if match.is_locked:
            return redirect(f'/cups/{cup.id}/?error=locked')

        existing_confirmation = CupMatchConfirmation.objects.select_for_update().filter(match=match, user=request.user).first()
        if existing_confirmation:
            return redirect(f'/cups/{cup.id}/?info=already_confirmed')

        decision = request.POST.get('decision')
        dispute_reason = (request.POST.get('dispute_reason') or '').strip()
        if decision not in ['accept', 'dispute']:
            return redirect(f'/cups/{cup.id}/?error=invalid_decision')
        if decision == 'dispute' and not dispute_reason:
            return redirect(f'/cups/{cup.id}/?error=dispute_reason_required')

        CupMatchConfirmation.objects.create(
            match=match,
            user=request.user,
            claimed_winner=match.winner,
            decision=decision,
            dispute_reason=dispute_reason
        )
        _log_cup_action(
            cup,
            request.user,
            'dual_confirm' if decision == 'accept' else 'player_dispute',
            message=f"{request.user.username} {'accepted' if decision == 'accept' else 'disputed'} result for R{match.round_number}M{match.match_number}",
            match=match,
            metadata={'decision': decision, 'dispute_reason': dispute_reason}
        )

        confirmations = list(CupMatchConfirmation.objects.select_for_update().filter(match=match))
        if any(c.decision == 'dispute' for c in confirmations):
            match.status = 'disputed'
            match.is_disputed = True
            first_reason = next((c.dispute_reason for c in confirmations if c.decision == 'dispute' and c.dispute_reason), '')
            if first_reason:
                match.dispute_reason = first_reason
            match.save(update_fields=['status', 'is_disputed', 'dispute_reason'])
            creator_profile = cup.creator.profile
            creator_profile.trust_score = max(0, creator_profile.trust_score - 2)
            creator_profile.save(update_fields=['trust_score'])
            _notify_cup_users([cup.creator_id], f'Dispute raised in {cup.name} for Round {match.round_number} Match {match.match_number}.', url=f'/cups/{cup.id}/disputes/')
            return redirect(f'/cups/{cup.id}/')

        if len(confirmations) >= 2 and all(c.decision == 'accept' for c in confirmations):
            match.status = 'completed'
            match.is_locked = True
            match.is_disputed = False
            match.dispute_reason = ''
            match.result_source = 'dual_confirmation'
            match.save(update_fields=['status', 'is_locked', 'is_disputed', 'dispute_reason', 'result_source'])
            _advance_cup_winner(match, actor=request.user)
    return redirect(f'/cups/{cup.id}/')


@login_required
@require_POST
def cup_player_action(request, cup_id):
    with transaction.atomic():
        cup = Cup.objects.select_for_update().get(id=cup_id)
        if request.user != cup.creator and not request.user.profile.is_admin:
            return redirect('/')
        if request.POST.get('confirm_action') != 'yes':
            return redirect(f'/cups/{cup.id}/?error=confirm_required')
        user_id = request.POST.get('user_id')
        action = request.POST.get('action')
        cp = get_object_or_404(CupParticipant.objects.select_for_update(), cup=cup, user_id=user_id)
        target = cp.user
        if (cup.bracket_generated or cup.is_bracket_generated) and action == 'kick':
            return redirect(f'/cups/{cup.id}/?error=kick_after_start')

        if action == 'kick':
            cp.kicked = True
            cp.save(update_fields=['kicked'])
            _log_cup_action(cup, request.user, 'kick_player', message=f'{target.username} kicked from cup.', target_user=target)
        elif action == 'ban':
            reason = (request.POST.get('reason') or '').strip()
            if not reason:
                return redirect(f'/cups/{cup.id}/?error=ban_reason_required')
            cp.banned = True
            cp.save(update_fields=['banned'])
            _log_cup_action(
                cup,
                request.user,
                'ban_player',
                message=f'{target.username} banned from cup. Reason: {reason}',
                target_user=target,
                metadata={'reason': reason}
            )
            active_match = CupMatch.objects.select_for_update().filter(
                cup=cup,
                status__in=['pending', 'awaiting_confirmation', 'disputed'],
                is_locked=False
            ).filter(Q(player1=target) | Q(player2=target)).order_by('round_number', 'match_number').first()
            if active_match:
                opponent = active_match.player2 if active_match.player1_id == target.id else active_match.player1
                if opponent:
                    _resolve_match_winner(
                        active_match,
                        winner_user=opponent,
                        winner_label=opponent.username,
                        source='admin_override' if request.user.profile.is_admin else 'creator_proof',
                        actor=request.user,
                        completed=True
                    )
    return redirect(f'/cups/{cup.id}/')


@login_required
@require_POST
def resolve_cup_dispute(request, match_id):
    with transaction.atomic():
        match = CupMatch.objects.select_for_update().select_related('cup', 'player1', 'player2').get(id=match_id)
        cup = match.cup
        if request.user != cup.creator and not request.user.profile.is_admin:
            return redirect('/')
        if match.is_locked:
            return redirect(f'/cups/{cup.id}/?error=locked')
        action = request.POST.get('action', 'winner')
        if action == 'cancel':
            reason = (request.POST.get('cancel_reason') or 'Cancelled during dispute resolution').strip()
            match.status = 'disputed'
            match.is_disputed = True
            match.dispute_reason = reason
            match.is_locked = True
            match.save(update_fields=['status', 'is_disputed', 'dispute_reason', 'is_locked'])
            creator_profile = cup.creator.profile
            creator_profile.trust_score = max(0, creator_profile.trust_score - 5)
            creator_profile.save(update_fields=['trust_score'])
            _log_cup_action(cup, request.user, 'resolve_dispute', message=f'Dispute cancelled by manager: {reason}', match=match)
            return redirect(f'/cups/{cup.id}/')

        previous_winner_id = match.winner_id
        winner_id = request.POST.get('winner_id')
        winner = User.objects.filter(id=winner_id).first()
        if winner not in [match.player1, match.player2]:
            return redirect(f'/cups/{cup.id}/?error=invalid_winner')

        match.winner = winner
        match.winner_label = winner.username
        match.status = 'completed'
        match.is_locked = True
        match.is_disputed = False
        match.result_source = 'admin_override' if request.user.profile.is_admin else 'creator_proof'
        match.save(update_fields=['winner', 'winner_label', 'status', 'is_locked', 'is_disputed', 'result_source'])
        _log_cup_action(
            cup,
            request.user,
            'resolve_dispute',
            message=f'Dispute resolved. Winner: {winner.username}',
            match=match,
            target_user=winner
        )
        if previous_winner_id and previous_winner_id != winner.id:
            creator_profile = cup.creator.profile
            creator_profile.trust_score = max(0, creator_profile.trust_score - 5)
            creator_profile.save(update_fields=['trust_score'])
        _advance_cup_winner(match, actor=request.user)
    return redirect(f'/cups/{cup.id}/')


@login_required
@require_POST
def unlock_cup_match(request, match_id):
    match = get_object_or_404(CupMatch, id=match_id)
    if not request.user.profile.is_admin:
        return redirect('/')
    match.is_locked = False
    match.status = 'awaiting_confirmation' if match.winner else 'pending'
    match.save(update_fields=['is_locked', 'status'])
    _log_cup_action(match.cup, request.user, 'admin_override', message=f'Admin unlocked R{match.round_number}M{match.match_number}', match=match)
    return redirect(f'/cups/{match.cup.id}/')


@login_required
@require_POST
def set_cup_match_deadline(request, match_id):
    match = get_object_or_404(CupMatch, id=match_id)
    cup = match.cup
    if request.user != cup.creator and not request.user.profile.is_admin:
        return redirect('/')
    timezone_choice = request.POST.get('timezone_choice', 'IST')
    deadline_raw = request.POST.get('deadline')
    deadline = parse_and_convert(deadline_raw, timezone_choice) if deadline_raw else None
    match.deadline = deadline
    match.save(update_fields=['deadline'])
    _log_cup_action(cup, request.user, 'admin_override', message=f'Deadline updated for R{match.round_number}M{match.match_number}', match=match)
    return redirect(f'/cups/{cup.id}/')


@login_required
def cup_state_api(request, cup_id):
    cup = get_object_or_404(Cup, id=cup_id)
    matches = cup.cup_matches.select_related('player1', 'player2', 'winner').all()
    rounds = sorted(set(matches.values_list('round_number', flat=True)))
    round_cards = [{'round': r, 'matches': list(matches.filter(round_number=r))} for r in rounds]
    bracket_html = render_to_string('partials/cup_bracket.html', {
        'round_cards': round_cards,
        'user': request.user,
        'can_manage': request.user == cup.creator or request.user.profile.is_admin,
        'cup': cup,
    }, request=request)
    stats_html = render_to_string('partials/cup_stats.html', {
        'count': cup.participants.filter(kicked=False, banned=False).count(),
        'total_matches': matches.count(),
        'completed_matches': matches.filter(status='completed').count(),
        'pending_matches': matches.filter(status='pending').count(),
        'awaiting_matches': matches.filter(status='awaiting_confirmation').count(),
        'disputed_matches_count': matches.filter(status='disputed').count(),
        'completion_pct': int((matches.filter(status='completed').count() * 100) / matches.count()) if matches.count() else 0,
    }, request=request)
    return JsonResponse({'ok': True, 'bracket_html': bracket_html, 'stats_html': stats_html})


# ─── ADMIN ─────────────────────────────────────────────────────────────────

@login_required
def creator_admin(request):
    if not request.user.profile.is_admin:
        return redirect('/')

    tournaments = Tournament.objects.all().order_by('-created_at')
    users = User.objects.all().order_by('-date_joined')
    withdrawal_requests = WithdrawalRequest.objects.all().order_by('-requested_at')
    reward_codes = RewardCode.objects.all().order_by('-created_at')
    memberships = CreatorMembership.objects.all().order_by('-started_at')
    all_transactions = Transaction.objects.all().order_by('-created_at')[:50]
    open_disputes = DisputeReport.objects.filter(status='open').order_by('-created_at')[:50]

    tournament_data = []
    for t in tournaments:
        sync_tournament_status(t)
        count = Participant.objects.filter(tournament=t).count()
        tournament_data.append({'tournament': t, 'count': count})

    return render(request, 'admin_panel.html', {
        'tournament_data': tournament_data,
        'users': users,
        'withdrawal_requests': withdrawal_requests,
        'reward_codes': reward_codes,
        'memberships': memberships,
        'all_transactions': all_transactions,
        'open_disputes': open_disputes,
    })


@login_required
@require_POST
def resolve_dispute(request, dispute_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    dispute = get_object_or_404(DisputeReport, id=dispute_id)
    action = request.POST.get('action')
    note = (request.POST.get('admin_note') or '').strip()

    if action == 'resolve':
        dispute.status = 'resolved'
    elif action == 'reject':
        dispute.status = 'rejected'
    else:
        return redirect('/creator-admin/')

    dispute.admin_note = note
    dispute.resolved_at = timezone.now()
    dispute.save(update_fields=['status', 'admin_note', 'resolved_at'])

    send_notification(
        dispute.user, 'general',
        f'🧾 Dispute {dispute.get_status_display()}',
        f'Your dispute on {dispute.tournament.name} is {dispute.get_status_display().lower()}.'
    )
    return redirect('/creator-admin/')


def terms_page(request):
    return render(request, 'terms.html')


def privacy_page(request):
    return render(request, 'privacy.html')


def refund_policy_page(request):
    return render(request, 'refund_policy.html')


def help_page(request):
    return render(request, 'help.html')


@login_required
def topup_wallet(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        u = get_object_or_404(User, id=user_id)
        amount = request.POST.get('amount')
        try:
            amount = Decimal(str(amount))
            if amount > 0:
                credit_wallet(
                    u, amount,
                    'admin_topup',
                    f'💰 ₹{amount} added to your wallet by Admin'
                )
                send_notification(
                    u, 'wallet_credit',
                    f'💰 Wallet Top Up — ₹{amount}',
                    f'₹{amount} has been added to your wallet by Admin. Check your balance!'
                )
        except Exception:
            pass
    return redirect('/creator-admin/')


@login_required
def approve_withdrawal(request, withdrawal_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    w = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
    w.status = 'approved'
    w.save()
    send_notification(
        w.user, 'wallet_credit',
        f'✅ Withdrawal Approved — ₹{w.amount}',
        f'Your withdrawal of ₹{w.amount} has been approved and sent to {w.upi_id}.'
    )
    return redirect('/creator-admin/')


@login_required
def reject_withdrawal(request, withdrawal_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    w = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
    credit_wallet(
        w.user, w.amount,
        'withdrawal_refund',
        f'♻️ Withdrawal rejected — ₹{w.amount} refunded to wallet'
    )
    send_notification(
        w.user, 'wallet_credit',
        f'♻️ Withdrawal Rejected — ₹{w.amount} Refunded',
        f'Your withdrawal of ₹{w.amount} was rejected. Amount has been refunded to your wallet.'
    )
    w.status = 'rejected'
    w.save()
    return redirect('/creator-admin/')


@login_required
def add_reward_code(request):
    if not request.user.profile.is_admin and not request.user.profile.is_creator:
        return redirect('/')
    if request.method == "POST":
        code = request.POST.get('code')
        description = request.POST.get('description', '')
        tournament_id = request.POST.get('tournament_id')
        tournament = None
        if tournament_id:
            tournament_qs = Tournament.objects.filter(id=tournament_id)
            if not request.user.profile.is_admin:
                tournament_qs = tournament_qs.filter(creator=request.user)
            tournament = tournament_qs.first()
        if code:
            if request.user.profile.is_creator and not request.user.profile.is_admin:
                if 'google' not in f"{code} {description}".lower() and 'play' not in f"{code} {description}".lower():
                    target = '/creator/rewards/'
                    if tournament_id:
                        target += f'?tournament_id={tournament_id}&reward_error=google_only'
                    return redirect(target)
            RewardCode.objects.create(
                code=code,
                description=description,
                tournament=tournament,
                sent_by=request.user
            )
    target = '/creator/rewards/'
    if tournament_id:
        target += f'?tournament_id={tournament_id}&code_added=1'
    return redirect(target)


@login_required
def tournament_participants_api(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    if not request.user.profile.is_admin and tournament.creator != request.user:
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    participants = Participant.objects.filter(tournament=tournament).select_related('user').order_by('user__username')
    return JsonResponse({
        'ok': True,
        'participants': [
            {'id': p.user_id, 'username': p.user.username, 'email': p.user.email}
            for p in participants
        ]
    })


@login_required
@require_POST
def send_reward_code(request, code_id=None):
    if not request.user.profile.is_admin and not request.user.profile.is_creator:
        return redirect('/')
    tournament_id = request.POST.get('tournament_id')
    single_user_id = request.POST.get('user_id')

    # Legacy/admin path: send a specific code to one user.
    if code_id is not None:
        code = get_object_or_404(RewardCode, id=code_id, sent=False)
        if not single_user_id:
            return redirect(request.META.get('HTTP_REFERER', '/'))

        user = get_object_or_404(User, id=single_user_id)
        tournament = None

        if request.user.profile.is_creator and not request.user.profile.is_admin:
            if not tournament_id:
                return redirect(request.META.get('HTTP_REFERER', '/'))
            tournament = get_object_or_404(Tournament, id=tournament_id, creator=request.user)
            if tournament.status != 'completed':
                return redirect(f"/tournament/{tournament.id}/?reward_error=not_completed")
            if not Participant.objects.filter(tournament=tournament, user=user).exists():
                return redirect(request.META.get('HTTP_REFERER', '/'))
            if not is_google_reward_code(code):
                return redirect(f"/tournament/{tournament.id}/?reward_error=google_only")
        elif tournament_id:
            tournament = Tournament.objects.filter(id=tournament_id).first()

        code.assigned_to = user
        code.tournament = tournament
        code.sent_by = request.user
        code.sent = True
        code.sent_at = timezone.now()
        code.save(update_fields=['assigned_to', 'tournament', 'sent_by', 'sent', 'sent_at'])

        rank_label = request.POST.get('rank') or 'Winner'
        if user.email:
            try:
                from .tasks import send_reward_code_email_task
                enqueue_task(
                    send_reward_code_email_task,
                    user.email,
                    user.username,
                    code.code,
                    code.description,
                    code.id,
                    tournament.name if tournament else 'Clash Arena Tournament',
                    rank_label
                )
            except Exception:
                logger.exception("Failed to enqueue reward email code_id=%s", code.id)
        send_notification(
            user,
            'general',
            'Reward Code Received',
            'You received a reward code. Check your email for full details.',
            tournament=tournament
        )
        send_notification(
            request.user,
            'general',
            '✅ Reward Sent Successfully',
            f'Reward code was sent to {user.username} for {tournament.name if tournament else "selected tournament"}.',
            tournament=tournament
        )
        return redirect(request.META.get('HTTP_REFERER', '/'))

    # Bulk creator flow: map tournament participants -> unsent codes.
    user_ids = request.POST.getlist('user_ids')
    if single_user_id and single_user_id not in user_ids:
        user_ids.append(single_user_id)
    if not tournament_id or not user_ids:
        return redirect('/creator/rewards/?reward_error=no_participants')

    tournament_qs = Tournament.objects.filter(id=tournament_id)
    if not request.user.profile.is_admin:
        tournament_qs = tournament_qs.filter(creator=request.user)
    tournament = get_object_or_404(tournament_qs)
    if request.user.profile.is_creator and not request.user.profile.is_admin and tournament.status != 'completed':
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=not_completed")

    if not check_rate_limit(f"reward_send_creator:{request.user.id}", limit=30, window_seconds=3600):
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=rate")

    eligible_users = list(User.objects.filter(id__in=user_ids, participant__tournament=tournament).distinct())
    if not eligible_users:
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=no_participants")

    incomplete_profiles = [
        u for u in eligible_users
        if not u.first_name or not u.email or not getattr(u.profile, 'upi_id', None)
    ]
    if incomplete_profiles:
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=incomplete_profiles")

    sent_count = RewardCode.objects.filter(tournament=tournament, sent=True).count()
    available_quota = MAX_REWARD_CODES_PER_TOURNAMENT - sent_count
    if available_quota <= 0:
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=maxed")

    users_to_reward = eligible_users[:available_quota]
    selected_code_ids = request.POST.getlist('code_ids')
    if not selected_code_ids:
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=no_code_selected")

    codes_qs = RewardCode.objects.filter(sent=False, tournament=tournament, id__in=selected_code_ids)
    if request.user.profile.is_creator and not request.user.profile.is_admin:
        codes_qs = codes_qs.filter(
            Q(description__icontains='google') |
            Q(description__icontains='play') |
            Q(code__icontains='google') |
            Q(code__icontains='play')
        )
    available_codes = list(codes_qs.order_by('created_at'))
    if not available_codes:
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=no_codes")

    pair_count = min(len(users_to_reward), len(available_codes))
    if pair_count < len(users_to_reward):
        return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_error=codes_short")

    for idx in range(pair_count):
        user = users_to_reward[idx]
        code = available_codes[idx]
        rank_label = request.POST.get(f'rank_{user.id}') or 'Winner'
        code.assigned_to = user
        code.tournament = tournament
        code.sent_by = request.user
        code.sent = True
        code.sent_at = timezone.now()
        code.save(update_fields=['assigned_to', 'tournament', 'sent_by', 'sent', 'sent_at'])
        if user.email:
            try:
                from .tasks import send_reward_code_email_task
                enqueue_task(
                    send_reward_code_email_task,
                    user.email,
                    user.username,
                    code.code,
                    code.description,
                    code.id,
                    tournament.name,
                    rank_label
                )
            except Exception:
                logger.exception("Failed to enqueue reward email code_id=%s", code.id)
        send_notification(
            user,
            'general',
            'Reward Code Received',
            f'You received a reward code for {tournament.name}. Check your email for full details.',
            tournament=tournament
        )

    send_notification(
        request.user,
        'general',
        '✅ Rewards Sent Successfully',
        f'{pair_count} reward code(s) were sent for {tournament.name}.',
        tournament=tournament
    )

    return redirect(f"/creator/rewards/?tournament_id={tournament.id}&reward_sent={pair_count}")


@login_required
def legacy_send_reward_code_redirect(request, code_ref):
    # Backward-compatible handler for old URLs like /send_reward_code/<code>/
    return redirect('/creator-admin/')


@login_required
@require_POST
def toggle_creator_follow(request, creator_id):
    creator = get_object_or_404(User, id=creator_id)
    if creator_id == request.user.id:
        return redirect('/')
    if not creator.profile.is_creator:
        return redirect('/')

    follow, created = CreatorFollow.objects.get_or_create(
        follower=request.user,
        creator=creator,
        defaults={'notifications_enabled': True}
    )
    if not created:
        follow.delete()
    return redirect(request.META.get('HTTP_REFERER', '/'))


@login_required
@require_POST
def toggle_follow_notifications(request, creator_id):
    follow = get_object_or_404(CreatorFollow, follower=request.user, creator_id=creator_id)
    follow.notifications_enabled = not follow.notifications_enabled
    follow.save(update_fields=['notifications_enabled'])
    return redirect(request.META.get('HTTP_REFERER', '/'))


def creators_view(request):
    active_creators = User.objects.filter(
        profile__is_creator=True,
        profile__plan_expiry__gt=timezone.now()
    ).select_related('profile').order_by('username')
    followed_creator_ids = set()
    follow_alert_on_ids = set()
    if request.user.is_authenticated:
        follows = CreatorFollow.objects.filter(follower=request.user)
        followed_creator_ids = set(follows.values_list('creator_id', flat=True))
        follow_alert_on_ids = set(
            follows.filter(notifications_enabled=True).values_list('creator_id', flat=True)
        )
    return render(request, 'creators.html', {
        'active_creators': active_creators,
        'followed_creator_ids': followed_creator_ids,
        'follow_alert_on_ids': follow_alert_on_ids,
    })


@login_required
def creator_rewards_view(request):
    if not request.user.profile.is_admin and not request.user.profile.is_creator:
        return redirect('/')

    tournaments_qs = Tournament.objects.all().order_by('-created_at')
    if not request.user.profile.is_admin:
        tournaments_qs = tournaments_qs.filter(creator=request.user)
    tournaments = list(tournaments_qs[:100])

    selected_tournament = None
    participants = []
    available_codes = RewardCode.objects.none()
    sent_codes = RewardCode.objects.none()
    if tournaments:
        selected_id = request.GET.get('tournament_id')
        if selected_id:
            selected_tournament = next((t for t in tournaments if str(t.id) == str(selected_id)), tournaments[0])
        else:
            selected_tournament = tournaments[0]
        participants = User.objects.filter(
            participant__tournament=selected_tournament
        ).select_related('profile').distinct().order_by('username')
        available_codes = RewardCode.objects.filter(
            tournament=selected_tournament,
            sent=False
        ).select_related('sent_by').order_by('-created_at')
        sent_codes = RewardCode.objects.filter(
            tournament=selected_tournament,
            sent=True
        ).select_related('assigned_to', 'sent_by').order_by('-sent_at', '-created_at')[:50]

    return render(request, 'creator_rewards.html', {
        'tournaments': tournaments,
        'selected_tournament': selected_tournament,
        'participants': participants,
        'available_codes': available_codes,
        'sent_codes': sent_codes,
        'reward_sent': request.GET.get('reward_sent'),
        'code_added': request.GET.get('code_added'),
        'reward_error': request.GET.get('reward_error'),
    })


@login_required
def grant_membership(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        u = get_object_or_404(User, id=user_id)
        plan = request.POST.get('plan')

        if plan == '1month':
            expiry = timezone.now() + timedelta(days=30)
        elif plan == '3month':
            expiry = timezone.now() + timedelta(days=90)
        elif plan == '1year':
            expiry = timezone.now() + timedelta(days=365)
        else:
            expiry = timezone.now() + timedelta(days=30)

        CreatorMembership.objects.create(user=u, plan=plan, expires_at=expiry)
        u.profile.is_creator = True
        u.profile.creator_plan = plan
        u.profile.plan_expiry = expiry
        u.profile.tournaments_created_this_month = 0
        u.profile.save()

        send_notification(
            u, 'general',
            '👑 Creator Membership Activated!',
            f'Your {plan} creator membership is now active! You can start hosting tournaments.'
        )

    return redirect('/creator-admin/')


@login_required
def promote_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    u.profile.is_admin = True
    u.profile.is_creator = True
    u.profile.save()
    return redirect('/creator-admin/')


@login_required
def demote_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    u.profile.is_admin = False
    u.profile.is_creator = False
    u.profile.save()
    return redirect('/creator-admin/')


@login_required
def ban_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    if u != request.user:
        u.is_active = False
        u.save()
    return redirect('/creator-admin/')


@login_required
def unban_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    u.is_active = True
    u.save()
    return redirect('/creator-admin/')


@login_required
def delete_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    if u != request.user:
        u.delete()
    return redirect('/creator-admin/')


@login_required
def deactivate_membership(request, membership_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    membership = get_object_or_404(CreatorMembership, id=membership_id)
    membership.is_active = False
    membership.save()
    membership.user.profile.creator_plan = 'none'
    membership.user.profile.plan_expiry = None
    membership.user.profile.is_creator = False
    membership.user.profile.save()
    send_notification(
        membership.user, 'general',
        '❌ Membership Deactivated',
        'Your creator membership has been deactivated by Admin. Contact support for more info.'
    )
    return redirect('/creator-admin/')


@login_required
def reactivate_membership(request, membership_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    membership = get_object_or_404(CreatorMembership, id=membership_id)
    membership.is_active = True
    membership.save()
    membership.user.profile.creator_plan = membership.plan
    membership.user.profile.plan_expiry = membership.expires_at
    membership.user.profile.is_creator = True
    membership.user.profile.save()
    send_notification(
        membership.user, 'general',
        '✅ Membership Reactivated!',
        f'Your {membership.plan} creator membership has been reactivated. You can host tournaments again!'
    )
    return redirect('/creator-admin/')


# ─── CUP MANAGEMENT (EDIT / DELETE) ────────────────────────────────────────

@login_required
def edit_cup(request, cup_id):
    """Allow cup creator or admin to edit cup details (before bracket generated)."""
    cup = get_object_or_404(Cup, id=cup_id)

    if request.user != cup.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == 'POST':
        timezone_choice = request.POST.get('timezone_choice', 'IST')
        cup.name = request.POST.get('name', cup.name).strip() or cup.name
        cup.prize_pool = request.POST.get('prize_pool') or cup.prize_pool
        cup.rules = request.POST.get('rules', cup.rules).strip()
        cup.eligibility_criteria = request.POST.get('eligibility_criteria', cup.eligibility_criteria).strip() or cup.eligibility_criteria
        cup.min_trophies = int(request.POST.get('min_trophies') or cup.min_trophies)
        cup.max_players = int(request.POST.get('max_players') or cup.max_players)
        start_raw = request.POST.get('start_time')
        end_raw = request.POST.get('end_time')
        if start_raw:
            cup.start_time = parse_and_convert(start_raw, timezone_choice)
        if end_raw:
            cup.end_time = parse_and_convert(end_raw, timezone_choice)
        cup.save()

        # Update join guide if present
        if hasattr(cup, 'join_guide'):
            cup.join_guide.clan_name = request.POST.get('clan_name', cup.join_guide.clan_name).strip()
            cup.join_guide.clan_tag = request.POST.get('clan_tag', cup.join_guide.clan_tag).strip()
            cup.join_guide.instructions = request.POST.get('instructions', cup.join_guide.instructions).strip()
            cup.join_guide.save()
        else:
            clan_name = request.POST.get('clan_name', '').strip()
            clan_tag = request.POST.get('clan_tag', '').strip()
            instructions = request.POST.get('instructions', '').strip()
            if clan_name:
                CupJoinGuide.objects.create(
                    cup=cup,
                    clan_name=clan_name,
                    clan_tag=clan_tag,
                    instructions=instructions,
                )
        _log_cup_action(cup, request.user, 'create_cup', message='Cup details updated by organizer.')
        return redirect(f'/cups/{cup.id}/')

    return render(request, 'edit_cup.html', {
        'cup': cup,
        'guide': getattr(cup, 'join_guide', None),
    })


@login_required
@require_POST
def delete_cup(request, cup_id):
    """Allow cup creator or admin to delete a cup."""
    cup = get_object_or_404(Cup, id=cup_id)

    if request.user != cup.creator and not request.user.profile.is_admin:
        return redirect('/')

    # Notify participants
    participant_ids = list(cup.participants.values_list('user_id', flat=True))
    if participant_ids:
        _notify_cup_users(
            participant_ids,
            f'Cup "{cup.name}" has been cancelled and deleted by the organizer.',
            url='/cups/'
        )

    cup.delete()
    return redirect('/cups/')


@login_required
def payment_page(request):
    """Dedicated UPI payment page for wallet top-up with QR code."""
    from django.conf import settings as django_settings
    admin_upi = getattr(django_settings, 'ADMIN_UPI_ID', 'manthanballa08@okicici')
    return render(request, 'payment_page.html', {
        'profile': request.user.profile,
        'cashfree_env': getattr(django_settings, 'CASHFREE_ENVIRONMENT', 'SANDBOX'),
    })


def contact_page(request):
    """Public Contact Us page for business verification."""
    return render(request, 'contact.html')