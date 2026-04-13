from django import forms
from django.contrib.auth.forms import SetPasswordForm


class NoReuseSetPasswordForm(SetPasswordForm):
    def clean(self):
        cleaned_data = super().clean()
        new_password1 = cleaned_data.get("new_password1")
        if new_password1 and self.user and self.user.check_password(new_password1):
            self.add_error("new_password1", "New password cannot be the same as your old password.")
        return cleaned_data
