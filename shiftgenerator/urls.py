from django.urls import path
from . import views
from django.contrib.auth import views as auth_views


app_name = 'shiftgenerator'

urlpatterns = [
    path('', views.index, name='index'),
    path('shift-form/', views.shift_form, name='shift-form'),
    path('api/shift-events/', views.shift_events_api, name='shift_events_api'),
    path('submit-shift/', views.submit_shift, name='submit-shift'),

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
    path('logout/', auth_views.LogoutView.as_view(next_page='shiftgenerator:index'), name='logout'),
    path('accounts/login/', auth_views.LoginView.as_view(), name='accounts-login'),
    path('login-manage/', views.login_manage, name='login-manage'),
    path('admin-home/', views.admin_home, name='admin-home'),
    path('staff-home/', views.staff_home, name='staff-home'),
    path('shift-management-view/', views.shift_management_view, name='shift-management-view'),
    path('shift-period-edit/<int:id>/', views.shift_period_edit, name='shift-period-edit'),
    path('shift-period-delete/<int:period_id>/', views.shift_period_delete, name='shift-period-delete'),
    path('shift-period-stop/<int:period_id>/', views.shift_period_stop,name='shift-period-stop'),
    path('shift-period-reopen/<int:period_id>/', views.shift_period_reopen, name='shift-period-reopen'),
    path('shift-period-publish/<int:period_id>/', views.shift_period_publish, name='shift-period-publish'),
    path('api/published-shift-events/', views.published_shift_events_api, name='published-shift-events'),
    path('api/published-period-meta/', views.published_period_meta_api, name='published-period-meta'),
    
    path('shift-management/', views.shift_management, name='shift-management'),
    path('shift-calendar-view/', views.shift_calendar_view, name='shift-calendar-view'),
    path('copy-shifts/', views.copy_shifts, name='copy-shifts'),
    path('save-shifts/', views.save_shifts, name='save-shifts'),
    path('save-shift-and-registers/', views.save_shift_and_registers, name='save-shift-and-registers'),
    path('save-register-assignments/', views.save_register_assignments, name='save-register-assignments'),
    path('delete-register-assignment/', views.delete_register_assignment, name='delete-register-assignment'),
    path('get-register-assignments/', views.get_register_assignments, name='get-register-assignments'),
    path('save-shift-and-registers/', views.save_shift_and_registers, name='save-shift-and-registers'),
    path('api/shift-items/', views.api_shift_items, name='api-shift-items'),
    
    path('staff-management/', views.staff_management, name='staff-management'),
    path('bulk-reset-to-wish/', views.bulk_reset_to_wish, name='bulk-reset-to-wish'),
    path('bulk-reset-preview/', views.bulk_reset_preview, name='bulk-reset-preview'),
    path('staff-disable/<int:staff_id>/', views.staff_disable, name='staff-disable'),
    path('staff-skill/<int:staff_id>/', views.staff_skill, name='staff-skill'),
    path('staff-password/<int:staff_id>/', views.staff_password, name='staff-password'),
    path('staff-toggle/<int:staff_id>/', views.staff_toggle_active, name='staff-toggle'),

    path('excel-export/', views.excel_export_dashboard, name='excel-export'),

]
