from django.urls import path
from .views import (
    home, login_view, register_view, logout_view, profile_view, my_tournaments_view,
    join_tournament, create_tournament, tournament_detail,
    generate_matches, submit_result, delete_tournament, edit_tournament,
    creator_admin, promote_user, demote_user, ban_user, unban_user,
    withdraw_view, approve_withdrawal, reject_withdrawal,
    tournament_rules, cancel_tournament, add_reward_code,
    send_reward_code, grant_membership, subscription_view,
    delete_user, deactivate_membership, reactivate_membership,
    topup_wallet, notifications_view, mark_notification_read, notifications_summary_api,
    upload_results,
    submit_dispute, resolve_dispute, tournament_participants_api,
    toggle_creator_follow, toggle_follow_notifications, creators_view, creator_rewards_view,
    terms_page, privacy_page, refund_policy_page, help_page, CustomPasswordResetConfirmView,
    legacy_send_reward_code_redirect, cups_view, create_cup, cup_detail, cup_dispute_queue, join_cup,
    generate_cup_matches, mark_cup_winner, confirm_cup_match_result, cup_player_action,
    resolve_cup_dispute, unlock_cup_match, set_cup_match_deadline, cup_state_api,
    edit_cup, delete_cup, payment_page, contact_page, adjust_trust_score,
    create_cashfree_order, cashfree_webhook, check_cashfree_status, api_mark_winner,
    admin_finance_dashboard
)
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.http import HttpResponse
from .sitemaps import StaticViewSitemap, TournamentSitemap, CupSitemap

sitemaps_dict = {
    'static': StaticViewSitemap,
    'tournaments': TournamentSitemap,
    'cups': CupSitemap,
}

def robots_txt(request):
    lines = [
        "User-Agent: *",
        "Disallow: /admin/",
        "Disallow: /creator-admin/",
        "Disallow: /api/",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")

urlpatterns = [
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps_dict}, name='sitemap'),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('', home, name='home'),
    path('login/', login_view, name='login'),
    path('register/', register_view, name='register'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('my-tournaments/', my_tournaments_view, name='my_tournaments'),
    path('withdraw/', withdraw_view, name='withdraw'),
    path('subscription/', subscription_view, name='subscription'),
    path('notifications/', notifications_view, name='notifications'),
    path('notifications/read/<int:notification_id>/', mark_notification_read, name='mark_read'),
    path('notifications/summary/', notifications_summary_api, name='notifications_summary_api'),

    # Cashfree Payment endpoints
    path('api/create-order/', create_cashfree_order, name='create_cashfree_order'),
    path('api/cashfree/webhook/', cashfree_webhook, name='cashfree_webhook'),
    path('api/check-payment-status/', check_cashfree_status, name='check_cashfree_status'),

    path('tournament/<int:tournament_id>/submit-dispute/', submit_dispute, name='submit_dispute'),
    path('resolve-dispute/<int:dispute_id>/', resolve_dispute, name='resolve_dispute'),
    path('terms/', terms_page, name='terms'),
    path('privacy/', privacy_page, name='privacy'),
    path('refund-policy/', refund_policy_page, name='refund_policy'),
    path('contact/', contact_page, name='contact'),
    path('help/', help_page, name='help'),

    path('join/<int:tournament_id>/', join_tournament, name='join_tournament'),
    path('rules/<int:tournament_id>/', tournament_rules, name='tournament_rules'),
    path('create-tournament/', create_tournament, name='create_tournament'),
    path('tournament/<int:tournament_id>/', tournament_detail, name='tournament_detail'),
    path('generate-matches/<int:tournament_id>/', generate_matches, name='generate_matches'),
    path('submit-result/<int:match_id>/', submit_result, name='submit_result'),
    path('api/match/mark-winner/', api_mark_winner, name='api_mark_winner'),
    path('delete-tournament/<int:tournament_id>/', delete_tournament, name='delete_tournament'),
    path('edit-tournament/<int:tournament_id>/', edit_tournament, name='edit_tournament'),
    path('cancel-tournament/<int:tournament_id>/', cancel_tournament, name='cancel_tournament'),
    path('upload-results/<int:tournament_id>/', upload_results, name='upload_results'),

    path('creator-admin/', creator_admin, name='creator_admin'),
    path('dashboard/finance/', admin_finance_dashboard, name='admin_finance'),
    path('adjust-trust-score/<int:user_id>/', adjust_trust_score, name='adjust_trust_score'),
    path('promote-user/<int:user_id>/', promote_user, name='promote_user'),
    path('demote-user/<int:user_id>/', demote_user, name='demote_user'),
    path('ban-user/<int:user_id>/', ban_user, name='ban_user'),
    path('unban-user/<int:user_id>/', unban_user, name='unban_user'),
    path('delete-user/<int:user_id>/', delete_user, name='delete_user'),
    path('topup-wallet/<int:user_id>/', topup_wallet, name='topup_wallet'),
    path('approve-withdrawal/<int:withdrawal_id>/', approve_withdrawal, name='approve_withdrawal'),
    path('reject-withdrawal/<int:withdrawal_id>/', reject_withdrawal, name='reject_withdrawal'),
    path('add-reward-code/', add_reward_code, name='add_reward_code'),
    path('send-reward-code/<int:code_id>/', send_reward_code, name='send_reward_code'),
    path('send-reward-code/', send_reward_code, name='send_reward_code_bulk'),
    path('send_reward_code/<str:code_ref>/', legacy_send_reward_code_redirect, name='legacy_send_reward_code_redirect'),
    path('creator/tournament-participants/<int:tournament_id>/', tournament_participants_api, name='tournament_participants_api'),
    path('creator/follow/<int:creator_id>/', toggle_creator_follow, name='toggle_creator_follow'),
    path('creator/follow-notifications/<int:creator_id>/', toggle_follow_notifications, name='toggle_follow_notifications'),
    path('creators/', creators_view, name='creators'),
    path('creator/rewards/', creator_rewards_view, name='creator_rewards'),
    path('cups/', cups_view, name='cups'),
    path('cups/create/', create_cup, name='create_cup'),
    path('cups/<int:cup_id>/', cup_detail, name='cup_detail'),
    path('cups/<int:cup_id>/disputes/', cup_dispute_queue, name='cup_dispute_queue'),
    path('cups/<int:cup_id>/join/', join_cup, name='join_cup'),
    path('cups/<int:cup_id>/generate/', generate_cup_matches, name='generate_cup_matches'),
    path('cups/match/<int:match_id>/winner/', mark_cup_winner, name='mark_cup_winner'),
    path('cups/match/<int:match_id>/confirm/', confirm_cup_match_result, name='confirm_cup_match_result'),
    path('cups/match/<int:match_id>/resolve/', resolve_cup_dispute, name='resolve_cup_dispute'),
    path('cups/match/<int:match_id>/unlock/', unlock_cup_match, name='unlock_cup_match'),
    path('cups/match/<int:match_id>/deadline/', set_cup_match_deadline, name='set_cup_match_deadline'),
    path('cups/<int:cup_id>/state/', cup_state_api, name='cup_state_api'),
    path('cups/<int:cup_id>/player-action/', cup_player_action, name='cup_player_action'),
    path('cups/<int:cup_id>/edit/', edit_cup, name='edit_cup'),
    path('cups/<int:cup_id>/delete/', delete_cup, name='delete_cup'),
    path('payments/pay/', payment_page, name='payment_page'),
    path('grant-membership/<int:user_id>/', grant_membership, name='grant_membership'),
    path('deactivate-membership/<int:membership_id>/', deactivate_membership, name='deactivate_membership'),
    path('reactivate-membership/<int:membership_id>/', reactivate_membership, name='reactivate_membership'),

    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='auth/password_reset.html',
            subject_template_name='auth/password_reset_subject.txt',
            email_template_name='auth/password_reset_email.txt',
            html_email_template_name='auth/password_reset_email.html',
        ),
        name='password_reset'
    ),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='auth/password_reset_done.html'), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', CustomPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(template_name='auth/password_reset_complete.html'), name='password_reset_complete'),
]