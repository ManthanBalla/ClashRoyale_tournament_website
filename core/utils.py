from django.db import models

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
