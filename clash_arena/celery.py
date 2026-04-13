import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'clash_arena.settings')

app = Celery('clash_arena')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
