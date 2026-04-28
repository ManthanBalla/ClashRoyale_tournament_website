from django.core.cache import cache

def check_rate_limit(key, limit=20, window_seconds=60):
    current = cache.get(key, 0)
    if current >= limit:
        return False
    if current == 0:
        cache.set(key, 1, timeout=window_seconds)
    else:
        cache.incr(key)
    return True

from .models import Transaction, Profile, Notification, Participant
from decimal import Decimal
from django.db import transaction

def add_transaction(user, transaction_type, reason, amount, description='', category=None, tournament=None, payment=None, reference_id=None, status='success'):
    if category is None:
        if reason in ('tournament_win',):
            category = 'winning'
        elif reason in ('tournament_refund', 'withdrawal_refund'):
            category = 'refund'
        elif transaction_type == 'debit':
            category = 'debit'
        else:
            category = 'credit'

    return Transaction.objects.create(
        user=user,
        transaction_type=transaction_type,
        category=category,
        reason=reason,
        amount=amount,
        status=status,
        tournament=tournament,
        payment=payment,
        reference_id=reference_id,
        description=description
    )

def credit_wallet(user, amount, reason, balance_type='deposit', description='', tournament=None, payment=None, reference_id=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Credit amount must be greater than zero.')

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        if balance_type == 'deposit':
            profile.deposit_balance += amount
        else:
            profile.winnings_balance += amount
        profile.save(update_fields=['deposit_balance', 'winnings_balance'])
        
        add_transaction(
            user, 'credit', reason, amount, description,
            tournament=tournament, payment=payment, reference_id=reference_id
        )

def debit_wallet(user, amount, reason, description='', tournament=None, payment=None, reference_id=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Debit amount must be greater than zero.')

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=user)
        if profile.total_balance < amount:
            raise ValueError('Insufficient wallet balance.')
        
        remaining = amount
        if profile.deposit_balance > 0:
            deduct_from_deposit = min(profile.deposit_balance, remaining)
            profile.deposit_balance -= deduct_from_deposit
            remaining -= deduct_from_deposit
        
        if remaining > 0:
            profile.winnings_balance -= remaining
            remaining = 0
            
        profile.save(update_fields=['deposit_balance', 'winnings_balance'])
        add_transaction(
            user, 'debit', reason, amount, description,
            tournament=tournament, payment=payment, reference_id=reference_id
        )

def send_notification(user, notification_type, title, message, tournament=None):
    return Notification.objects.create(
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

def update_trust_score(user, amount):
    """
    Updates the user's trust score, ensuring it stays between 0 and 200.
    """
    if not user or not hasattr(user, 'profile'):
        return

    profile = user.profile
    profile.trust_score += amount
    
    # Enforce bounds
    if profile.trust_score < 0:
        profile.trust_score = 0
    elif profile.trust_score > 200:
        profile.trust_score = 200
        
    profile.save()


import os
from PIL import Image, ImageDraw, ImageFont
from django.conf import settings
import cloudinary.uploader
from io import BytesIO

def generate_winner_certificate(user, tournament=None, cup=None):
    """
    Generates a high-quality certificate image for the tournament or cup winner.
    """
    # Background image path
    bg_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'certificate_bg.png')
    if not os.path.exists(bg_path):
        # Fallback to a plain dark background if image missing
        img = Image.new('RGB', (1200, 800), color=(10, 10, 15))
    else:
        img = Image.open(bg_path)
    
    draw = ImageDraw.Draw(img)
    
    # Font settings
    try:
        # Try common Windows/Linux font paths
        font_paths = [
            "C:\\Windows\\Fonts\\arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:\\Windows\\Fonts\\arial.ttf"
        ]
        font_path = next((p for p in font_paths if os.path.exists(p)), None)
        if font_path:
            title_font = ImageFont.truetype(font_path, 60)
            name_font = ImageFont.truetype(font_path, 85)
            detail_font = ImageFont.truetype(font_path, 40)
            seal_font = ImageFont.truetype(font_path, 30)
        else:
            raise Exception("No font found")
    except:
        title_font = ImageFont.load_default()
        name_font = ImageFont.load_default()
        detail_font = ImageFont.load_default()
        seal_font = ImageFont.load_default()

    # Colors
    gold_color = (255, 215, 0)
    white_color = (255, 255, 255)
    
    event_name = tournament.name if tournament else cup.name
    event_date = (tournament.end_time or tournament.start_time) if tournament else (cup.end_time or cup.start_time)
    date_str = event_date.strftime('%d %b %Y') if event_date else '2026'

    # Text rendering (Centered)
    draw.text((600, 180), "OFFICIAL CERTIFICATE OF VICTORY", font=title_font, fill=gold_color, anchor="mm")
    draw.text((600, 360), f"{user.username}", font=name_font, fill=white_color, anchor="mm")
    draw.text((600, 480), f"Winner of {event_name}", font=detail_font, fill=white_color, anchor="mm")
    draw.text((600, 560), f"Issued on {date_str}", font=detail_font, fill=(180, 180, 180), anchor="mm")
    
    # Seal / Brand
    draw.rectangle([450, 650, 750, 720], outline=gold_color, width=3)
    draw.text((600, 685), "VERIFIED BY CLASH ARENA", font=seal_font, fill=gold_color, anchor="mm")

    # Save to buffer
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    # Upload to Cloudinary
    try:
        folder = "certificates/"
        public_id = f"cert_{'t' if tournament else 'c'}_{tournament.id if tournament else cup.id}_{user.id}"
        upload_result = cloudinary.uploader.upload(
            buffer,
            folder=folder,
            public_id=public_id,
            overwrite=True
        )
        cert_url = upload_result.get('secure_url')
        
        # Save to database
        from .models import WinnerCertificate
        WinnerCertificate.objects.create(
            user=user,
            tournament=tournament,
            cup=cup,
            image_url=cert_url
        )
        return cert_url
    except Exception as e:
        print(f"Error generating/uploading certificate: {e}")
        return None
