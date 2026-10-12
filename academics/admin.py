from django.contrib import admin

from .models import Clazz, Course


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    """后台课程管理"""
    list_display = ('code', 'name')
    search_fields = ('code', 'name')


@admin.register(Clazz)
class ClazzAdmin(admin.ModelAdmin):
    """后台班级管理"""
    list_display = ('name',)
    search_fields = ('name',)
