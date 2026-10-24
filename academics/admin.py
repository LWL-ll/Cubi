from django.contrib import admin
from django import forms

from .services import validateTeachingClasses

from .models import (AdminClass, ClassMembership, Course, CourseCollege,
                     Department, Major, TeachingClass)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    """后台学院管理"""
    list_display = ('code', 'name', 'is_active')
    search_fields = ('code', 'name')


@admin.register(Major)
class MajorAdmin(admin.ModelAdmin):
    """后台专业管理"""
    list_display = ('code', 'name', 'department', 'is_active')
    list_filter = ('department', 'is_active')
    search_fields = ('code', 'name')


@admin.register(AdminClass)
class AdminClassAdmin(admin.ModelAdmin):
    """后台行政班级管理"""
    list_display = ('name', 'major', 'enrollment_year')
    list_filter = ('major',)
    search_fields = ('name',)


@admin.register(ClassMembership)
class ClassMembershipAdmin(admin.ModelAdmin):
    """后台班级成员管理"""
    list_display = ('student_profile', 'admin_class', 'joined_at', 'left_at')
    list_filter = ('admin_class',)
    search_fields = ('student_profile__identity__school_number',)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    """后台课程管理"""
    list_display = ('code', 'name')
    search_fields = ('code', 'name')


@admin.register(CourseCollege)
class CourseCollegeAdmin(admin.ModelAdmin):
    """后台课程×学院管理"""
    list_display = ('course', 'department', 'leader')
    list_filter = ('department',)
    search_fields = ('course__name', 'department__name')


class TeachingClassForm(forms.ModelForm):
    class Meta:
        model = TeachingClass
        fields = '__all__'

    def clean(self):
        data = super().clean()
        cc = data.get('course_college')
        classes = data.get('admin_classes')
        if cc and data.get('term') and classes is not None:
            if not classes:
                self.add_error('admin_classes', '教学班至少包含一个行政班')
            else:
                validateTeachingClasses(cc, data['term'], classes,
                                        excludeId=self.instance.pk)
        return data


@admin.register(TeachingClass)
class TeachingClassAdmin(admin.ModelAdmin):
    """后台教学班管理"""
    list_display = ('name', 'term', 'course_college')
    list_filter = ('term',)
    search_fields = ('name',)
    form = TeachingClassForm
