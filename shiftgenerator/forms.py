# forms.py
from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import CustomUser, Staff
from .models import ShiftPreference

class ShiftPreferenceForm(forms.ModelForm):
    class Meta:
        model = ShiftPreference
        fields = ['staff', 'starttime', 'endtime', 'day_of_week']  # 必要なフィールドを指定
        widgets = {
            'starttime': forms.TimeInput(format='%H:%M'),
            'endtime': forms.TimeInput(format='%H:%M'),
            'day_of_week': forms.Select(),
        }
        labels = {
            'staff': 'スタッフ',
            'starttime': '開始時間',
            'endtime': '終了時間',
            'day_of_week': '曜日',
        }

class CustomUserCreationForm(UserCreationForm):
    name = forms.CharField(max_length=255, help_text='スタッフ名')

    class Meta:
        model = CustomUser
        fields = ('username', 'password1', 'password2', 'name')

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
            Staff.objects.create(
                name=self.cleaned_data['name'],
                custom_user=user
            )
        return user
