from django.urls import path
from . import views
from django.contrib.auth import views as auth_views


app_name = 'shiftgenerator'

urlpatterns = [
    path('', views.index, name='index'),
    path('shift-form/', views.shift_form, name='shift-form'),
    path('shift-register/', views.shift_register, name='shift-register'),
    path('new-register-shift/', views.new_register_shift, name='new-register-shift'),
    path('shift-detail/<int:shift_id>/', views.shift_detail, name='shift-detail'),
    path('get-shift-history/', views.get_shift_history, name='get-shift-history'),
    path('get-holidays/', views.get_holidays, name='get-holidays'),
    path('history-shift-register/', views.history_shift_register, name='history-shift-register'),
    path('holiday-shift-register/', views.holiday_shift_register, name='holiday-shift-register'),
    path('get-update-events/', views.get_update_events, name='get-update-events'),
    path('register/', views.register, name='register'),
    path('login/', views.login, name='login'), 
    path('accounts/login/', auth_views.LoginView.as_view(), name='accounts-login'),
    path('login-manage/', views.login_manage, name='login-manage'),
    path('admin-home/', views.admin_home, name='admin-home'),
    path('staff-home/', views.staff_home, name='staff-home'),
    path('shift-management-view/', views.shift_management_view, name='shift-management-view'),
    path('shift-management-summary/', views.shift_management_summary, name='shift-management-summary'),
    path('shift-management/', views.shift_management, name='shift-management'),
    path('shift-calendar-view/', views.shift_calendar_view, name='shift-calendar-view'),
    path('copy-shifts/', views.copy_shifts, name='copy-shifts'),
    path('save-shifts/', views.save_shifts, name='save-shifts'),
    
    path('staff-management/', views.staff_management, name='staff-management'),
    path('staff-disable/<int:staff_id>/', views.staff_disable, name='staff-disable'),
    path('staff-skill/<int:staff_id>/', views.staff_skill, name='staff-skill'),
    path('staff-password/<int:staff_id>/', views.staff_password, name='staff-password'),
    path('staff-toggle/<int:staff_id>/', views.staff_toggle_active, name='staff-toggle'),



]
