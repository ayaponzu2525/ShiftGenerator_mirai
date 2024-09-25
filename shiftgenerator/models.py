from django.db import models

'''
<Staff>
id: スタッフを一意に識別するためのプライマリーキー。自動増分。
name: スタッフの名前。
created_at: スタッフが登録された日時。自動的に設定される。
updated_at: スタッフ情報が最後に更新された日時。自動的に更新される。

<Skill>
id: スキルを一意に識別するためのプライマリーキー。自動増分。
skill_name: スキルの名前（例: "レジ"、"品出し" など）。

<StaffSkill>
id
staff_id: Staff テーブルへの外部キー。どのスタッフがどのスキルを持っているかを管理する。
skill_id: Skill テーブルへの外部キー。スタッフが持っているスキルを表す。
created_at: スタッフにスキルが登録された日時。自動的に設定される。
updated_at: スタッフのスキル情報が最後に更新された日時。自動的に更新される。

<DayOfWeek>
day_number: 曜日の番号 (0: 月曜日, 6: 日曜日)
day_name :曜日


<ShiftPreference>
id
staff_id: Staff テーブルへの外部キー。スタッフのシフト希望を表す。
starttime: 希望シフトの開始時刻。
endtime: 希望シフトの終了時刻。
day_of_week: DayofWeekへの外部キー。曜日を表す。
created_at: シフト希望が登録された日時。自動的に設定される。
updated_at: シフト希望が最後に更新された日時。自動的に更新される。

<ShiftHistory>
id
staff_id: Staff テーブルへの外部キー。
starttime: 履歴シフトの開始時刻。
endtime: 履歴シフトの終了時刻。
created_at: レコードが作成された日時。自動的に設定される。
updated_at: レコードが最後に更新された日時。自動的に更新される。
'''

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
import uuid

class CustomUserManager(BaseUserManager):
    def create_user(self, username, password=None, **extra_fields):
        if not username:
            raise ValueError('The Username field must be set')
        user = self.model(username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(username, password, **extra_fields)

# shiftgenerator/models.py

class CustomUser(AbstractBaseUser, PermissionsMixin):
    id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return self.username

class Staff(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    custom_user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='staff_profile', null=True, blank=True)

    def __str__(self):
        return self.name


class Skill(models.Model):
    id = models.AutoField(primary_key=True)
    skill_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.skill_name

class StaffSkill(models.Model):
    id = models.AutoField(primary_key=True)
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('staff', 'skill')

    def __str__(self):
        return f'{self.staff.name} - {self.skill.skill_name}'

class DayOfWeek(models.Model):
    day_number = models.IntegerField(unique=True)  # 曜日の番号 (0: 月曜日, 6: 日曜日)
    day_name = models.CharField(max_length=9)      # 曜日の名前 (例: "Monday", "Sunday")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.day_name

class Holiday(models.Model):
    HOLIDAY_CHOICES = [
        ('休み', '休み'),
        ('未定', '未定'),
        ('学校関連', '学校関連'),
    ]

    name = models.CharField(max_length=10, choices=HOLIDAY_CHOICES)

    def __str__(self):
        return self.name

class ShiftPreference(models.Model):
    id = models.AutoField(primary_key=True)
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    date = models.DateField()
    starttime = models.TimeField(null=True)
    endtime = models.TimeField(null=True)
    confirmed_starttime = models.TimeField(null=True, blank=True)
    confirmed_endtime = models.TimeField(null=True, blank=True)
    day_of_week = models.ForeignKey(DayOfWeek, on_delete=models.CASCADE)
    holiday = models.ForeignKey(Holiday, on_delete=models.SET_NULL, null=True, blank=True)  # 追加
    description = models.TextField(blank=True)  # 一言テキストを入れるためのカラム
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        holiday_name = self.holiday.name if self.holiday else 'なし'  # holidayが設定されていない場合の処理
        return (f'{self.staff.name} - {self.day_of_week.day_name} {self.date} '
                f'{self.starttime} to {self.endtime} | 休み: {holiday_name} | 説明: {self.description}')

class ShiftHistory(models.Model):
    id = models.AutoField(primary_key=True)
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    starttime = models.TimeField()
    endtime = models.TimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.staff.name} - {self.starttime} to {self.endtime}'
