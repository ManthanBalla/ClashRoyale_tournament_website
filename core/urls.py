from django.urls import path
from .views import home, login_view, register_view, logout_view, profile_view, join_tournament,create_tournament, tournament_detail,generate_matches,submit_result
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('', home, name='home'),
    path('login/', login_view, name='login'),
    path('register/', register_view, name='register'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),

    # JOIN
    path('join/<int:tournament_id>/', join_tournament, name='join_tournament'),
    path('create-tournament/', create_tournament, name='create_tournament'),
    path('tournament/<int:tournament_id>/', tournament_detail, name='tournament_detail'),
    path('generate-matches/<int:tournament_id>/', generate_matches, name='generate_matches'),
    path('submit-result/<int:match_id>/', submit_result, name='submit_result'),

    # PASSWORD RESET
    path('password-reset/', auth_views.PasswordResetView.as_view(template_name='auth/password_reset.html'), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='auth/password_reset_done.html'), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(template_name='auth/password_reset_confirm.html'), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(template_name='auth/password_reset_complete.html'), name='password_reset_complete'),
]