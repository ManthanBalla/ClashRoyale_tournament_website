from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
import random

from .models import Tournament, Participant, Match


def home(request):
    tournaments = Tournament.objects.all().order_by('-created_at')

    joined_tournaments = []
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(user=request.user).values_list('tournament_id', flat=True)

    tournament_data = []
    for t in tournaments:
        count = Participant.objects.filter(tournament=t).count()
        tournament_data.append({
            'tournament': t,
            'count': count
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
            return render(request, 'auth/register.html', {'error': 'Username already exists'})

        user = User.objects.create_user(username=username, email=email, password=password)
        login(request, user)
        return redirect('/')

    return render(request, 'auth/register.html')


def logout_view(request):
    logout(request)
    return redirect('/')


@login_required
def profile_view(request):
    return render(request, 'profile.html')


@login_required
def join_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect('/')

    if tournament.password:
        if request.method == "POST":
            entered_password = request.POST.get('password')

            if entered_password != tournament.password:
                return render(request, 'enter_password.html', {
                    'tournament': tournament,
                    'error': 'Wrong password'
                })

            Participant.objects.create(user=request.user, tournament=tournament)
            return redirect('/')

        return render(request, 'enter_password.html', {'tournament': tournament})

    Participant.objects.create(user=request.user, tournament=tournament)
    return redirect('/')


@login_required
def create_tournament(request):
    if not request.user.profile.is_creator:
        return redirect('/')

    if request.method == "POST":
        name = request.POST['name']
        description = request.POST['description']
        password = request.POST.get('password') or None
        reward = request.POST.get('reward', '')
        start_time = request.POST.get('start_time')
        proof_image = request.FILES.get('proof_image')

        Tournament.objects.create(
            name=name,
            description=description,
            password=password,
            reward=reward,
            start_time=start_time,
            proof_image=proof_image,
            creator=request.user
        )

        return redirect('/')

    return render(request, 'create_tournament.html')


from django.utils.timezone import localtime

@login_required
def join_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    now = localtime()

    show_password = False
    if tournament.start_time:
        if now >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect('/')

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
def generate_matches(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    participants = list(
        Participant.objects.filter(tournament=tournament).values_list('user', flat=True)
    )

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

    return redirect(f'/tournament/{tournament.id}/')


@login_required
def submit_result(request, match_id):
    match = get_object_or_404(Match, id=match_id)

    if request.method == "POST":
        winner_id = request.POST.get('winner')
        winner = User.objects.get(id=winner_id)

        match.winner = winner
        match.save()

    return redirect(f'/tournament/{match.tournament.id}/')

from django.utils.timezone import localtime
from datetime import timedelta

def tournament_detail(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    participants = Participant.objects.filter(tournament=tournament)
    matches = Match.objects.filter(tournament=tournament)

    now = localtime()

    show_password = False
    if tournament.start_time:
        if now >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    joined_tournaments = []
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)

    return render(request, 'tournament_detail.html', {
        'tournament': tournament,
        'participants': participants,
        'matches': matches,
        'show_password': show_password,
        'joined_tournaments': joined_tournaments
    })

@login_required
def delete_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator:
        return redirect('/')

    if request.method == "POST":
        tournament.delete()
        return redirect('/')

    return redirect('/')


@login_required
def edit_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator:
        return redirect('/')

    if request.method == "POST":
        tournament.name = request.POST['name']
        tournament.description = request.POST['description']
        tournament.password = request.POST.get('password') or None
        tournament.reward = request.POST.get('reward', '')
        tournament.start_time = request.POST.get('start_time')

        if request.FILES.get('proof_image'):
            tournament.proof_image = request.FILES.get('proof_image')

        tournament.save()
        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'edit_tournament.html', {'tournament': tournament})