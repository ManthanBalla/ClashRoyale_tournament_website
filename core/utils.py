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

def add_transaction(user, transaction_type, reason, amount, description='', category=None, tournament=None, payment=None, reference_id=None, status='success', balance_after=None):
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
        balance_after=balance_after,
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
            new_balance = profile.deposit_balance
        else:
            profile.winnings_balance += amount
            new_balance = profile.winnings_balance
        profile.save(update_fields=['deposit_balance', 'winnings_balance'])
        
        add_transaction(
            user, 'credit', reason, amount, description,
            tournament=tournament, payment=payment, reference_id=reference_id,
            balance_after=new_balance
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
        # Record the effective balance after debit (total of both wallets)
        balance_after = profile.deposit_balance + profile.winnings_balance
        add_transaction(
            user, 'debit', reason, amount, description,
            tournament=tournament, payment=payment, reference_id=reference_id,
            balance_after=balance_after
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

def _get_font(font_path, size):
    """Safely load a TrueType font with fallback to default."""
    try:
        if font_path:
            return ImageFont.truetype(font_path, size)
    except Exception:
        pass
    return ImageFont.load_default()

def _fit_text_font(font_path, text, max_width, max_size, min_size=20):
    """
    Dynamically scale font size so text fits within max_width pixels.
    Prevents text from ever overflowing the certificate canvas.
    """
    for size in range(max_size, min_size - 1, -2):
        font = _get_font(font_path, size)
        bbox = font.getbbox(text)
        text_width = bbox[2] - bbox[0]
        if text_width <= max_width:
            return font
    return _get_font(font_path, min_size)

def generate_winner_certificate(user, tournament=None, cup=None):
    """
    Generates a high-quality, responsive certificate image for the tournament or cup winner.
    Text dynamically scales to always fit inside the certificate canvas.
    Uses display_name (in-game name) for player identification.
    """
    # Canvas dimensions
    W, H = 1200, 800
    
    # Safe text zone: 80px padding on each side = 1040px max text width
    SAFE_WIDTH = W - 160

    # Background image path
    bg_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'certificate_bg.png')
    if not os.path.exists(bg_path):
        img = Image.new('RGB', (W, H), color=(10, 10, 15))
    else:
        img = Image.open(bg_path).resize((W, H), Image.LANCZOS)
    
    draw = ImageDraw.Draw(img)
    
    # Semi-transparent dark overlay behind text for readability
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [60, 120, W - 60, H - 80],
        radius=20,
        fill=(0, 0, 0, 140)
    )
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
    draw = ImageDraw.Draw(img)
    
    # Resolve font path
    font_paths = [
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    font_path = next((p for p in font_paths if os.path.exists(p)), None)

    # Colors
    gold = (255, 215, 0)
    white = (255, 255, 255)
    silver = (180, 180, 180)
    
    # Player & event info
    player_name = user.profile.display_name if hasattr(user, 'profile') else user.username
    event_name = tournament.name if tournament else cup.name
    event_date = (tournament.end_time or tournament.start_time) if tournament else (cup.end_time or cup.start_time)
    date_str = event_date.strftime('%d %b %Y') if event_date else '2026'

    # Dynamically sized fonts (auto-shrink to fit)
    title_text = "OFFICIAL CERTIFICATE OF VICTORY"
    title_font = _fit_text_font(font_path, title_text, SAFE_WIDTH, max_size=48, min_size=24)
    
    name_font = _fit_text_font(font_path, player_name, SAFE_WIDTH, max_size=72, min_size=28)
    
    event_text = f"Winner of {event_name}"
    event_font = _fit_text_font(font_path, event_text, SAFE_WIDTH, max_size=36, min_size=18)
    
    date_text = f"Issued on {date_str}"
    date_font = _fit_text_font(font_path, date_text, SAFE_WIDTH, max_size=30, min_size=16)
    
    seal_text = "VERIFIED BY CLASH ARENA"
    seal_font = _fit_text_font(font_path, seal_text, 280, max_size=22, min_size=12)

    # Centered text rendering (Y positions spread evenly across safe zone)
    cx = W // 2  # Center X = 600
    
    draw.text((cx, 200), title_text, font=title_font, fill=gold, anchor="mm")
    draw.text((cx, 370), player_name, font=name_font, fill=white, anchor="mm")
    draw.text((cx, 480), event_text, font=event_font, fill=white, anchor="mm")
    draw.text((cx, 550), date_text, font=date_font, fill=silver, anchor="mm")
    
    # Seal
    draw.rounded_rectangle([cx - 160, 630, cx + 160, 690], radius=8, outline=gold, width=2)
    draw.text((cx, 660), seal_text, font=seal_font, fill=gold, anchor="mm")

    # Save to buffer
    buffer = BytesIO()
    img.save(buffer, format="PNG", quality=95)
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
