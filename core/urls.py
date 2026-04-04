from django.urls import path
from .views import (
    home, login_view, register_view, logout_view, profile_view,
    join_tournament, create_tournament, tournament_detail,
    generate_matches, submit_result, delete_tournament, edit_tournament,
    creator_admin, promote_user, demote_user, ban_user, unban_user,
    withdraw_view, approve_withdrawal, reject_withdrawal,
    tournament_rules, cancel_tournament, add_reward_code,
    send_reward_code, grant_membership
)
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('', home, name='home'),
    path('login/', login_view, name='login'),
    path('register/', register_view, name='register'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('withdraw/', withdraw_view, name='withdraw'),

    path('join/<int:tournament_id>/', join_tournament, name='join_tournament'),
    path('rules/<int:tournament_id>/', tournament_rules, name='tournament_rules'),
    path('create-tournament/', create_tournament, name='create_tournament'),
    path('tournament/<int:tournament_id>/', tournament_detail, name='tournament_detail'),
    path('generate-matches/<int:tournament_id>/', generate_matches, name='generate_matches'),
    path('submit-result/<int:match_id>/', submit_result, name='submit_result'),
    path('delete-tournament/<int:tournament_id>/', delete_tournament, name='delete_tournament'),
    path('edit-tournament/<int:tournament_id>/', edit_tournament, name='edit_tournament'),
    path('cancel-tournament/<int:tournament_id>/', cancel_tournament, name='cancel_tournament'),

    path('creator-admin/', creator_admin, name='creator_admin'),
    path('promote-user/<int:user_id>/', promote_user, name='promote_user'),
    path('demote-user/<int:user_id>/', demote_user, name='demote_user'),
    path('ban-user/<int:user_id>/', ban_user, name='ban_user'),
    path('unban-user/<int:user_id>/', unban_user, name='unban_user'),
    path('approve-withdrawal/<int:withdrawal_id>/', approve_withdrawal, name='approve_withdrawal'),
    path('reject-withdrawal/<int:withdrawal_id>/', reject_withdrawal, name='reject_withdrawal'),
    path('add-reward-code/', add_reward_code, name='add_reward_code'),
    path('send-reward-code/<int:code_id>/', send_reward_code, name='send_reward_code'),
    path('grant-membership/<int:user_id>/', grant_membership, name='grant_membership'),

    path('password-reset/', auth_views.PasswordResetView.as_view(template_name='auth/password_reset.html'), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='auth/password_reset_done.html'), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(template_name='auth/password_reset_confirm.html'), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(template_name='auth/password_reset_complete.html'), name='password_reset_complete'),
]