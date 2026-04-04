from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.timezone import localtime
from django.core.mail import send_mail
from datetime import timedelta
import random

from .models import Tournament, Participant, Match, Profile, WithdrawalRequest, RewardCode, CreatorMembership


def home(request):
    tournaments = Tournament.objects.exclude(status='cancelled').order_by('-created_at')
    joined_tournaments = []
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)

    tournament_data = []
    for t in tournaments:
        count = Participant.objects.filter(tournament=t).count()
        tournament_data.append({
            'tournament': t,
            'count': count,
            'prize_pool': t.entry_fee * count if t.is_paid else None
        })

    return render(request, 'home.html', {
        'tournament_data': tournament_data,
        'joined_tournaments': joined_tournaments
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
            return render(request, 'auth/register.html', {'error': 'Username already taken. Please choose a different username.'})

        if User.objects.filter(email=email).exists():
            return render(request, 'auth/register.html', {'error': 'An account with this email already exists.'})

        if not email:
            return render(request, 'auth/register.html', {'error': 'Email is required.'})

        user = User.objects.create_user(username=username, email=email, password=password)
        login(request, user)
        return redirect('/')

    return render(request, 'auth/register.html')


def logout_view(request):
    logout(request)
    return redirect('/')


@login_required
def profile_view(request):
    profile = request.user.profile
    withdrawal_requests = WithdrawalRequest.objects.filter(
        user=request.user
    ).order_by('-requested_at')
    memberships = CreatorMembership.objects.filter(
        user=request.user
    ).order_by('-started_at')

    if request.method == "POST":
        first_name = request.POST.get('first_name', '').strip()
        email = request.POST.get('email', '').strip()
        upi_id = request.POST.get('upi_id', '').strip()

        if email and email != request.user.email:
            if User.objects.filter(email=email).exclude(id=request.user.id).exists():
                return render(request, 'profile.html', {
                    'profile': profile,
                    'withdrawal_requests': withdrawal_requests,
                    'memberships': memberships,
                    'error': 'This email is already used by another account.'
                })

        request.user.first_name = first_name
        request.user.email = email
        request.user.save()
        profile.upi_id = upi_id
        profile.save()

        return render(request, 'profile.html', {
            'profile': profile,
            'withdrawal_requests': withdrawal_requests,
            'memberships': memberships,
            'success': 'Profile updated successfully!'
        })

    return render(request, 'profile.html', {
        'profile': profile,
        'withdrawal_requests': withdrawal_requests,
        'memberships': memberships,
    })


@login_required
def withdraw_view(request):
    profile = request.user.profile

    if not profile.is_complete():
        return redirect('/profile/?incomplete=1')

    if request.method == "POST":
        amount = request.POST.get('amount')
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return render(request, 'withdraw.html', {
                'error': 'Invalid amount.', 'profile': profile
            })

        if amount <= 0:
            return render(request, 'withdraw.html', {
                'error': 'Amount must be greater than 0.', 'profile': profile
            })

        if amount > float(profile.reward_balance):
            return render(request, 'withdraw.html', {
                'error': f'Insufficient balance. Your balance is ₹{profile.reward_balance}.',
                'profile': profile
            })

        WithdrawalRequest.objects.create(
            user=request.user,
            amount=amount,
            upi_id=profile.upi_id
        )
        profile.reward_balance -= amount
        profile.save()
        return redirect('/profile/?withdrawn=1')

    return render(request, 'withdraw.html', {'profile': profile})


@login_required
def tournament_rules(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    count = Participant.objects.filter(tournament=tournament).count()
    prize_pool = tournament.entry_fee * count if tournament.is_paid else None

    # if already joined redirect to detail
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
    now = localtime()

    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect('/')

    # for paid tournaments show rules first
    if tournament.is_paid:
        if request.method == "POST":
            agreed = request.POST.get('agreed')
            if not agreed:
                return redirect(f'/rules/{tournament.id}/')

            # check balance
            profile = request.user.profile
            if profile.reward_balance < tournament.entry_fee:
                return render(request, 'tournament_rules.html', {
                    'tournament': tournament,
                    'count': Participant.objects.filter(tournament=tournament).count(),
                    'prize_pool': tournament.entry_fee * Participant.objects.filter(tournament=tournament).count(),
                    'error': f'Insufficient balance. You need ₹{tournament.entry_fee} to join. Your balance is ₹{profile.reward_balance}.'
                })

            # deduct fee
            profile.reward_balance -= tournament.entry_fee
            profile.save()

            # update prize pool
            tournament.prize_pool += tournament.entry_fee
            tournament.save()

            Participant.objects.create(
                user=request.user,
                tournament=tournament,
                fee_paid=True
            )
            return redirect(f'/tournament/{tournament.id}/')

        return redirect(f'/rules/{tournament.id}/')

    # free tournament
    show_password = False
    if tournament.start_time:
        if now >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    if tournament.password:
        if request.method == "POST" and show_password:
            entered_password = request.POST.get('password')
            if entered_password != tournament.password:
                return render(request, 'enter_password.html', {
                    'tournament': tournament,
                    'error': 'Wrong password',
                    'show_password': show_password
                })
            Participant.objects.create(user=request.user, tournament=tournament)
            return redirect('/')
        return render(request, 'enter_password.html', {
            'tournament': tournament,
            'show_password': show_password
        })

    Participant.objects.create(user=request.user, tournament=tournament)
    return redirect('/')


@login_required
def create_tournament(request):
    profile = request.user.profile

    if not profile.is_admin and not profile.is_creator:
        return redirect('/')

    # only non-admin creators have limits
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
        start_time = request.POST.get('start_time')
        proof_image = request.FILES.get('proof_image')
        is_paid = request.POST.get('is_paid') == 'on'
        entry_fee = request.POST.get('entry_fee', 0) or 0
        min_players = request.POST.get('min_players', 2) or 2

        t = Tournament.objects.create(
            name=name,
            description=description,
            rules=rules,
            password=password,
            reward=reward,
            start_time=start_time,
            proof_image=proof_image,
            creator=request.user,
            is_paid=is_paid,
            entry_fee=entry_fee,
            min_players=min_players,
        )

        # increment usage for non-admin creators
        if not profile.is_admin:
            profile.tournaments_created_this_month += 1
            profile.save()

        return redirect('/')

    return render(request, 'create_tournament.html')


@login_required
def delete_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        # refund all paid participants
        if tournament.is_paid:
            participants = Participant.objects.filter(tournament=tournament, fee_paid=True)
            for p in participants:
                p.user.profile.reward_balance += tournament.entry_fee
                p.user.profile.save()

        tournament.delete()
        return redirect('/')

    return redirect('/')


@login_required
def edit_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        tournament.name = request.POST['name']
        tournament.description = request.POST['description']
        tournament.rules = request.POST.get('rules', '')
        tournament.password = request.POST.get('password') or None
        tournament.reward = request.POST.get('reward', '')
        tournament.start_time = request.POST.get('start_time')
        tournament.min_players = request.POST.get('min_players', 2) or 2
        if request.FILES.get('proof_image'):
            tournament.proof_image = request.FILES.get('proof_image')
        tournament.save()
        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'edit_tournament.html', {'tournament': tournament})


def tournament_detail(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    participants = Participant.objects.filter(tournament=tournament)
    matches = Match.objects.filter(tournament=tournament)
    now = localtime()
    count = participants.count()

    show_password = False
    if tournament.start_time:
        if now >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    joined_tournaments = []
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)

    prize_pool = tournament.entry_fee * count if tournament.is_paid else None

    return render(request, 'tournament_detail.html', {
        'tournament': tournament,
        'participants': participants,
        'matches': matches,
        'show_password': show_password,
        'joined_tournaments': joined_tournaments,
        'count': count,
        'prize_pool': prize_pool
    })


@login_required
def cancel_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        # refund all paid participants
        if tournament.is_paid:
            participants = Participant.objects.filter(tournament=tournament, fee_paid=True)
            for p in participants:
                p.user.profile.reward_balance += tournament.entry_fee
                p.user.profile.save()

        tournament.status = 'cancelled'
        tournament.save()
        return redirect('/')

    return redirect('/')


@login_required
def generate_matches(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    participants = list(
        Participant.objects.filter(tournament=tournament).values_list('user', flat=True)
    )

    # check minimum players
    if len(participants) < tournament.min_players:
        # cancel and refund if paid
        if tournament.is_paid:
            for uid in participants:
                u = User.objects.get(id=uid)
                u.profile.reward_balance += tournament.entry_fee
                u.profile.save()
            tournament.status = 'cancelled'
            tournament.save()
            return redirect(f'/tournament/{tournament.id}/?cancelled=1')
        return redirect(f'/tournament/{tournament.id}/?notenough=1')

    random.shuffle(participants)
    Match.objects.filter(tournament=tournament).delete()

    for i in range(0, len(participants), 2):
        if i + 1 < len(participants):
            Match.objects.create(
                tournament=tournament,
                player1_id=participants[i],
                player2_id=participants[i + 1],
                round_number=1
            )

    tournament.status = 'ongoing'
    tournament.save()
    return redirect(f'/tournament/{tournament.id}/')


@login_required
def submit_result(request, match_id):
    match = get_object_or_404(Match, id=match_id)
    if request.method == "POST":
        winner_id = request.POST.get('winner')
        winner = User.objects.get(id=winner_id)
        match.winner = winner
        match.save()

        # check if all matches done — award prize
        tournament = match.tournament
        all_matches = Match.objects.filter(tournament=tournament)
        if all_matches.count() > 0 and all(m.winner for m in all_matches):
            if tournament.is_paid and tournament.status == 'ongoing':
                # find overall winner (most wins)
                from collections import Counter
                wins = Counter(m.winner_id for m in all_matches if m.winner)
                top_winner_id = wins.most_common(1)[0][0]
                top_winner = User.objects.get(id=top_winner_id)
                top_winner.profile.reward_balance += tournament.prize_pool
                top_winner.profile.save()
                tournament.status = 'completed'
                tournament.save()

    return redirect(f'/tournament/{match.tournament.id}/')


@login_required
def creator_admin(request):
    if not request.user.profile.is_admin:
        return redirect('/')

    tournaments = Tournament.objects.all().order_by('-created_at')
    users = User.objects.all().order_by('-date_joined')
    withdrawal_requests = WithdrawalRequest.objects.all().order_by('-requested_at')
    reward_codes = RewardCode.objects.all().order_by('-created_at')
    memberships = CreatorMembership.objects.all().order_by('-started_at')

    tournament_data = []
    for t in tournaments:
        count = Participant.objects.filter(tournament=t).count()
        tournament_data.append({
            'tournament': t,
            'count': count
        })

    return render(request, 'admin_panel.html', {
        'tournament_data': tournament_data,
        'users': users,
        'withdrawal_requests': withdrawal_requests,
        'reward_codes': reward_codes,
        'memberships': memberships,
    })


@login_required
def approve_withdrawal(request, withdrawal_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    w = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
    w.status = 'approved'
    w.save()
    return redirect('/creator-admin/')


@login_required
def reject_withdrawal(request, withdrawal_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    w = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
    w.user.profile.reward_balance += w.amount
    w.user.profile.save()
    w.status = 'rejected'
    w.save()
    return redirect('/creator-admin/')


@login_required
def add_reward_code(request):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        code = request.POST.get('code')
        description = request.POST.get('description', '')
        RewardCode.objects.create(code=code, description=description)
    return redirect('/creator-admin/')


@login_required
def send_reward_code(request, code_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        code = get_object_or_404(RewardCode, id=code_id)
        user_id = request.POST.get('user_id')
        user = get_object_or_404(User, id=user_id)

        code.assigned_to = user
        code.sent = True
        code.save()

        # send email
        try:
            send_mail(
                subject='🎁 Your Reward Code - Clash Arena',
                message=f'Hi {user.username},\n\nYour reward code is: {code.code}\n\nDescription: {code.description}\n\nThank you for playing on Clash Arena!',
                from_email=None,
                recipient_list=[user.email],
            )
        except Exception:
            pass

    return redirect('/creator-admin/')


@login_required
def grant_membership(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        u = get_object_or_404(User, id=user_id)
        plan = request.POST.get('plan')

        if plan == '1month':
            expiry = timezone.now() + timedelta(days=30)
        else:
            expiry = timezone.now() + timedelta(days=90)

        CreatorMembership.objects.create(
            user=u,
            plan=plan,
            expires_at=expiry
        )

        u.profile.is_creator = True
        u.profile.creator_plan = plan
        u.profile.plan_expiry = expiry
        u.profile.tournaments_created_this_month = 0
        u.profile.save()

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