from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from .models import CustomUser, Staff, Skill, StaffSkill, DayOfWeek, ShiftPreference, ShiftHistory, Holiday
from .models import ShiftSubmissionPeriod, ShiftSubmission


class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = CustomUser
        fields = ('username', 'is_active', 'is_staff', 'is_superuser')

class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ('username',)

class CustomUserAdmin(UserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    model = CustomUser
    list_display = ('username', 'is_active', 'is_staff', 'is_superuser')
    list_filter = ('is_active', 'is_staff', 'is_superuser')
    fieldsets = (
        (None, {'fields': ('username', 'password')}), 
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser')}), 
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'password1', 'password2', 'is_active', 'is_staff', 'is_superuser'),
        }),
    )
    search_fields = ('username',)
    ordering = ('username',)

class HolidayAdmin(admin.ModelAdmin):
    list_display = ('id', 'holiday_name', 'created_at', 'updated_at')  # IDと名前を表示

@admin.register(ShiftPreference)
class ShiftPreferenceAdmin(admin.ModelAdmin):
    list_display = ('id', 'staff', 'date', 'starttime', 'endtime', 'holiday')  # 表示するフィールドを指定
    list_filter = ('holiday', 'staff')  # フィルターを追加
    search_fields = ('staff__name', 'description')  # スタッフ名と説明で検索可能

class ShiftSubmissionPeriodAdmin(admin.ModelAdmin):
    list_display = ('label', 'type', 'start_date', 'start_time', 'end_date', 'end_time', 'is_active')
    list_filter = ('type', 'is_active')
    search_fields = ('label',)

admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(Staff)
admin.site.register(Skill)
admin.site.register(StaffSkill)
admin.site.register(DayOfWeek) 
admin.site.register(ShiftHistory)
admin.site.register(Holiday, HolidayAdmin)
admin.site.register(ShiftSubmissionPeriod)
admin.site.register(ShiftSubmission)
