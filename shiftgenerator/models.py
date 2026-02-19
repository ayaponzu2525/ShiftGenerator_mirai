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
from datetime import datetime
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.core.validators import RegexValidator
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
    
excel_code_validator = RegexValidator(
    regex=r'^[a-z]{1,3}$',
    message='excel_code は a〜z（小文字）を 1〜3 文字で入力してください（例: a, m, aa）'
)

class Staff(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    custom_user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='staff_profile', null=True, blank=True)
    excel_code = models.CharField(
        max_length=3, null=True, blank=True,
        validators=[excel_code_validator],
        help_text="Excel列コード（例: a, b, ... , m, n, ...）"
    )
    
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
    id = models.AutoField(primary_key=True)
    holiday_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.holiday_name


class ShiftPreference(models.Model):
    id = models.AutoField(primary_key=True)

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    date = models.DateField()  # 日付を追加
    starttime = models.TimeField(null=True, blank=True)
    endtime = models.TimeField(null=True, blank=True)
    confirmed_starttime = models.TimeField(null=True, blank=True)  # 確定開始時刻
    confirmed_endtime = models.TimeField(null=True, blank=True)  # 確定終了時刻
    
    published_starttime = models.TimeField(null=True, blank=True) # 公開開始時刻
    published_endtime   = models.TimeField(null=True, blank=True) # 公開終了時刻
    published_at        = models.DateTimeField(null=True, blank=True) # 公開日時
    
    day_of_week = models.ForeignKey(DayOfWeek, on_delete=models.CASCADE)  # DayOfWeek モデルとの関連付け
    holiday = models.ForeignKey(Holiday, on_delete=models.SET_NULL, null=True, blank=True)  # Holiday モデルとの関連付け

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        holiday_name = self.holiday.holiday_name if self.holiday else 'なし'
        return f'{self.staff.name} - {self.day_of_week.day_name} {self.date} {self.starttime} to {self.endtime} ({holiday_name})'


class ShiftRegisterAssignment(models.Model):
    id = models.AutoField(primary_key=True)

    # ShiftPreference との関連
    shift = models.ForeignKey(ShiftPreference, on_delete=models.CASCADE, related_name='register_assignments')

    # レジ番号 (例: 1, 2, 3, 4)
    register_number = models.IntegerField()

    # 絶対時間
    register_start_time = models.TimeField(null=True, blank=True)
    register_end_time   = models.TimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        # 絶対時間で表示
        s = self.register_start_time.strftime('%H:%M') if self.register_start_time else '--:--'
        e = self.register_end_time.strftime('%H:%M') if self.register_end_time else '--:--'
        return f"{self.shift.staff.name} {self.shift.date} レジ{self.register_number}: {s}～{e}"


class ShiftHistory(models.Model):
    id = models.AutoField(primary_key=True)
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    starttime = models.TimeField()
    endtime = models.TimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.staff.name} - {self.starttime} to {self.endtime}'

# シフト提出期間設定用
class ShiftSubmissionPeriod(models.Model):
    TYPE_CHOICES = [
        ('default', 'デフォルト'),
        ('temporary', '臨時'),
        ('HELP', 'ヘルプ'),
    ]
    label = models.CharField(max_length=64, blank=True)  # blank=Trueで空欄許可
    start_date = models.DateField()
    end_date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)  # 開始時刻
    end_time = models.TimeField(null=True, blank=True)    # 終了時刻
    type = models.CharField(max_length=16, choices=TYPE_CHOICES, default='default')
    auto_close_date = models.DateTimeField(null=True, blank=True)  # 締切日時。日付だけならDateFieldでもOK
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # start_date, end_dateがstr型の場合は変換
        if isinstance(self.start_date, str):
            self.start_date = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        if isinstance(self.end_date, str):
            self.end_date = datetime.strptime(self.end_date, "%Y-%m-%d").date()
        if not self.label:
            self.label = f"{self.start_date.strftime('%Y年%m月%d日')}～{self.end_date.strftime('%m月%d日')}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.start_date.strftime('%Y年%m月%d日')}～{self.end_date.strftime('%m月%d日')}"
    
    
class ShiftSubmission(models.Model):
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['staff', 'period'],
                name='uniq_submission_staff_period'
            )
        ]
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE)
    period = models.ForeignKey(ShiftSubmissionPeriod, on_delete=models.CASCADE)
    # ShiftPreferenceは複数なのでM2MでもOK。まずは提出レコードとして1:多で設計
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    comment = models.TextField(blank=True, null=True)      # 再提出理由など
    submission_status = models.CharField(
        max_length=16,
        choices=[
            ('初回', '初回'), 
            ('再提出', '再提出'),
        ],
        default='初回'
    )
    snapshot = models.JSONField(blank=True, null=True)

    def __str__(self):
        return f"{self.staff.name} {self.period.label} {self.submission_status}"


class ExcelExportTemplate(models.Model):
    """
    Excel出力用テンプレート（差し替え可能）
    1件だけ使う想定（最新を使用）
    """
    file = models.FileField(upload_to='excel_templates/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"ExcelTemplate {self.id} ({self.uploaded_at})"