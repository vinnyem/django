from __future__ import unicode_literals

from datetime import date, datetime
import time
import unittest

from django.core.exceptions import FieldError
from django.db import models
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import Author, MySQLUnixTimestamp


class Div3Lookup(models.Lookup):
    lookup_name = 'div3'

    def as_sql(self, compiler, connection):
        lhs, params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params.extend(rhs_params)
        return '(%s) %%%% 3 = %s' % (lhs, rhs), params

    def as_oracle(self, compiler, connection):
        lhs, params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params.extend(rhs_params)
        return 'mod(%s, 3) = %s' % (lhs, rhs), params


class Div3Transform(models.Transform):
    lookup_name = 'div3'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = compiler.compile(self.lhs)
        return '(%s) %%%% 3' % lhs, lhs_params

    def as_oracle(self, compiler, connection):
        lhs, lhs_params = compiler.compile(self.lhs)
        return 'mod(%s, 3)' % lhs, lhs_params


class Div3BilateralTransform(Div3Transform):
    bilateral = True


class Mult3BilateralTransform(models.Transform):
    bilateral = True
    lookup_name = 'mult3'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = compiler.compile(self.lhs)
        return '3 * (%s)' % lhs, lhs_params


class UpperBilateralTransform(models.Transform):
    bilateral = True
    lookup_name = 'upper'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = compiler.compile(self.lhs)
        return 'UPPER(%s)' % lhs, lhs_params


class YearTransform(models.Transform):
    lookup_name = 'year'

    def as_sql(self, compiler, connection):
        lhs_sql, params = compiler.compile(self.lhs)
        return connection.ops.date_extract_sql('year', lhs_sql), params

    @property
    def output_field(self):
        return models.IntegerField()


@YearTransform.register_lookup
class YearExact(models.lookups.Lookup):
    lookup_name = 'exact'

    def as_sql(self, compiler, connection):
        # We will need to skip the extract part, and instead go
        # directly with the originating field, that is self.lhs.lhs
        lhs_sql, lhs_params = self.process_lhs(compiler, connection, self.lhs.lhs)
        rhs_sql, rhs_params = self.process_rhs(compiler, connection)
        # Note that we must be careful so that we have params in the
        # same order as we have the parts in the SQL.
        params = lhs_params + rhs_params + lhs_params + rhs_params
        # We use PostgreSQL specific SQL here. Note that we must do the
        # conversions in SQL instead of in Python to support F() references.
        return ("%(lhs)s >= (%(rhs)s || '-01-01')::date "
                "AND %(lhs)s <= (%(rhs)s || '-12-31')::date" %
                {'lhs': lhs_sql, 'rhs': rhs_sql}, params)


@YearTransform.register_lookup
class YearLte(models.lookups.LessThanOrEqual):
    """
    The purpose of this lookup is to efficiently compare the year of the field.
    """

    def as_sql(self, compiler, connection):
        # Skip the YearTransform above us (no possibility for efficient
        # lookup otherwise).
        real_lhs = self.lhs.lhs
        lhs_sql, params = self.process_lhs(compiler, connection, real_lhs)
        rhs_sql, rhs_params = self.process_rhs(compiler, connection)
        params.extend(rhs_params)
        # Build SQL where the integer year is concatenated with last month
        # and day, then convert that to date. (We try to have SQL like:
        #     WHERE somecol <= '2013-12-31')
        # but also make it work if the rhs_sql is field reference.
        return "%s <= (%s || '-12-31')::date" % (lhs_sql, rhs_sql), params


class SQLFunc(models.Lookup):
    def __init__(self, name, *args, **kwargs):
        super(SQLFunc, self).__init__(*args, **kwargs)
        self.name = name

    def as_sql(self, compiler, connection):
        return '%s()', [self.name]

    @property
    def output_field(self):
        return CustomField()


class SQLFuncFactory(object):

    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        return SQLFunc(self.name, *args, **kwargs)


class CustomField(models.TextField):

    def get_lookup(self, lookup_name):
        if lookup_name.startswith('lookupfunc_'):
            key, name = lookup_name.split('_', 1)
            return SQLFuncFactory(name)
        return super(CustomField, self).get_lookup(lookup_name)

    def get_transform(self, lookup_name):
        if lookup_name.startswith('transformfunc_'):
            key, name = lookup_name.split('_', 1)
            return SQLFuncFactory(name)
        return super(CustomField, self).get_transform(lookup_name)


class CustomModel(models.Model):
    field = CustomField()


# We will register this class temporarily in the test method.


class InMonth(models.lookups.Lookup):
    """
    InMonth matches if the column's month is the same as value's month.
    """
    lookup_name = 'inmonth'

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        # We need to be careful so that we get the params in right
        # places.
        params = lhs_params + rhs_params + lhs_params + rhs_params
        return ("%s >= date_trunc('month', %s) and "
                "%s < date_trunc('month', %s) + interval '1 months'" %
                (lhs, rhs, lhs, rhs), params)


class DateTimeTransform(models.Transform):
    lookup_name = 'as_datetime'

    @property
    def output_field(self):
        return models.DateTimeField()

    def as_sql(self, compiler, connection):
        lhs, params = compiler.compile(self.lhs)
        return 'from_unixtime({})'.format(lhs), params


class LookupTests(TestCase):
    def test_basic_lookup(self):
        a1 = Author.objects.create(name='a1', age=1)
        a2 = Author.objects.create(name='a2', age=2)
        a3 = Author.objects.create(name='a3', age=3)
        a4 = Author.objects.create(name='a4', age=4)
        models.IntegerField.register_lookup(Div3Lookup)
        try:
            self.assertQuerysetEqual(
                Author.objects.filter(age__div3=0),
                [a3], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(age__div3=1).order_by('age'),
                [a1, a4], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(age__div3=2),
                [a2], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(age__div3=3),
                [], lambda x: x
            )
        finally:
            models.IntegerField._unregister_lookup(Div3Lookup)

    @unittest.skipUnless(connection.vendor == 'postgresql', "PostgreSQL specific SQL used")
    def test_birthdate_month(self):
        a1 = Author.objects.create(name='a1', birthdate=date(1981, 2, 16))
        a2 = Author.objects.create(name='a2', birthdate=date(2012, 2, 29))
        a3 = Author.objects.create(name='a3', birthdate=date(2012, 1, 31))
        a4 = Author.objects.create(name='a4', birthdate=date(2012, 3, 1))
        models.DateField.register_lookup(InMonth)
        try:
            self.assertQuerysetEqual(
                Author.objects.filter(birthdate__inmonth=date(2012, 1, 15)),
                [a3], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(birthdate__inmonth=date(2012, 2, 1)),
                [a2], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(birthdate__inmonth=date(1981, 2, 28)),
                [a1], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(birthdate__inmonth=date(2012, 3, 12)),
                [a4], lambda x: x
            )
            self.assertQuerysetEqual(
                Author.objects.filter(birthdate__inmonth=date(2012, 4, 1)),
                [], lambda x: x
            )
        finally:
            models.DateField._unregister_lookup(InMonth)

    def test_div3_extract(self):
        models.IntegerField.register_lookup(Div3Transform)
        try:
            a1 = Author.objects.create(name='a1', age=1)
            a2 = Author.objects.create(name='a2', age=2)
            a3 = Author.objects.create(name='a3', age=3)
            a4 = Author.objects.create(name='a4', age=4)
            baseqs = Author.objects.order_by('name')
            self.assertQuerysetEqual(
                baseqs.filter(age__div3=2),
                [a2], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__lte=3),
                [a1, a2, a3, a4], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__in=[0, 2]),
                [a2, a3], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__in=[2, 4]),
                [a2], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__gte=3),
                [], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__range=(1, 2)),
                [a1, a2, a4], lambda x: x)
        finally:
            models.IntegerField._unregister_lookup(Div3Transform)


class BilateralTransformTests(TestCase):

    def test_bilateral_upper(self):
        models.CharField.register_lookup(UpperBilateralTransform)
        try:
            Author.objects.bulk_create([
                Author(name='Doe'),
                Author(name='doe'),
                Author(name='Foo'),
            ])
            self.assertQuerysetEqual(
                Author.objects.filter(name__upper='doe'),
                ["<Author: Doe>", "<Author: doe>"], ordered=False)
            self.assertQuerysetEqual(
                Author.objects.filter(name__upper__contains='f'),
                ["<Author: Foo>"], ordered=False)
        finally:
            models.CharField._unregister_lookup(UpperBilateralTransform)

    def test_bilateral_inner_qs(self):
        models.CharField.register_lookup(UpperBilateralTransform)
        try:
            with self.assertRaises(NotImplementedError):
                Author.objects.filter(name__upper__in=Author.objects.values_list('name'))
        finally:
            models.CharField._unregister_lookup(UpperBilateralTransform)

    def test_div3_bilateral_extract(self):
        models.IntegerField.register_lookup(Div3BilateralTransform)
        try:
            a1 = Author.objects.create(name='a1', age=1)
            a2 = Author.objects.create(name='a2', age=2)
            a3 = Author.objects.create(name='a3', age=3)
            a4 = Author.objects.create(name='a4', age=4)
            baseqs = Author.objects.order_by('name')
            self.assertQuerysetEqual(
                baseqs.filter(age__div3=2),
                [a2], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__lte=3),
                [a3], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__in=[0, 2]),
                [a2, a3], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__in=[2, 4]),
                [a1, a2, a4], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__gte=3),
                [a1, a2, a3, a4], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__range=(1, 2)),
                [a1, a2, a4], lambda x: x)
        finally:
            models.IntegerField._unregister_lookup(Div3BilateralTransform)

    def test_bilateral_order(self):
        models.IntegerField.register_lookup(Mult3BilateralTransform)
        models.IntegerField.register_lookup(Div3BilateralTransform)
        try:
            a1 = Author.objects.create(name='a1', age=1)
            a2 = Author.objects.create(name='a2', age=2)
            a3 = Author.objects.create(name='a3', age=3)
            a4 = Author.objects.create(name='a4', age=4)
            baseqs = Author.objects.order_by('name')

            self.assertQuerysetEqual(
                baseqs.filter(age__mult3__div3=42),
                # mult3__div3 always leads to 0
                [a1, a2, a3, a4], lambda x: x)
            self.assertQuerysetEqual(
                baseqs.filter(age__div3__mult3=42),
                [a3], lambda x: x)
        finally:
            models.IntegerField._unregister_lookup(Mult3BilateralTransform)
            models.IntegerField._unregister_lookup(Div3BilateralTransform)

    def test_bilateral_fexpr(self):
        models.IntegerField.register_lookup(Mult3BilateralTransform)
        try:
            a1 = Author.objects.create(name='a1', age=1, average_rating=3.2)
            a2 = Author.objects.create(name='a2', age=2, average_rating=0.5)
            a3 = Author.objects.create(name='a3', age=3, average_rating=1.5)
            a4 = Author.objects.create(name='a4', age=4)
            baseqs = Author.objects.order_by('name')
            self.assertQuerysetEqual(
                baseqs.filter(age__mult3=models.F('age')),
                [a1, a2, a3, a4], lambda x: x)
            self.assertQuerysetEqual(
                # Same as age >= average_rating
                baseqs.filter(age__mult3__gte=models.F('average_rating')),
                [a2, a3], lambda x: x)
        finally:
            models.IntegerField._unregister_lookup(Mult3BilateralTransform)


@override_settings(USE_TZ=True)
class DateTimeLookupTests(TestCase):
    @unittest.skipUnless(connection.vendor == 'mysql', "MySQL specific SQL used")
    def test_datetime_output_field(self):
        models.PositiveIntegerField.register_lookup(DateTimeTransform)
        try:
            ut = MySQLUnixTimestamp.objects.create(timestamp=time.time())
            y2k = timezone.make_aware(datetime(2000, 1, 1))
            self.assertQuerysetEqual(
                MySQLUnixTimestamp.objects.filter(timestamp__as_datetime__gt=y2k),
                [ut], lambda x: x)
        finally:
            models.PositiveIntegerField._unregister_lookup(DateTimeTransform)


class YearLteTests(TestCase):
    def setUp(self):
        models.DateField.register_lookup(YearTransform)
        self.a1 = Author.objects.create(name='a1', birthdate=date(1981, 2, 16))
        self.a2 = Author.objects.create(name='a2', birthdate=date(2012, 2, 29))
        self.a3 = Author.objects.create(name='a3', birthdate=date(2012, 1, 31))
        self.a4 = Author.objects.create(name='a4', birthdate=date(2012, 3, 1))

    def tearDown(self):
        models.DateField._unregister_lookup(YearTransform)

    @unittest.skipUnless(connection.vendor == 'postgresql', "PostgreSQL specific SQL used")
    def test_year_lte(self):
        baseqs = Author.objects.order_by('name')
        self.assertQuerysetEqual(
            baseqs.filter(birthdate__year__lte=2012),
            [self.a1, self.a2, self.a3, self.a4], lambda x: x)
        self.assertQuerysetEqual(
            baseqs.filter(birthdate__year=2012),
            [self.a2, self.a3, self.a4], lambda x: x)

        self.assertNotIn('BETWEEN', str(baseqs.filter(birthdate__year=2012).query))
        self.assertQuerysetEqual(
            baseqs.filter(birthdate__year__lte=2011),
            [self.a1], lambda x: x)
        # The non-optimized version works, too.
        self.assertQuerysetEqual(
            baseqs.filter(birthdate__year__lt=2012),
            [self.a1], lambda x: x)

    @unittest.skipUnless(connection.vendor == 'postgresql', "PostgreSQL specific SQL used")
    def test_year_lte_fexpr(self):
        self.a2.age = 2011
        self.a2.save()
        self.a3.age = 2012
        self.a3.save()
        self.a4.age = 2013
        self.a4.save()
        baseqs = Author.objects.order_by('name')
        self.assertQuerysetEqual(
            baseqs.filter(birthdate__year__lte=models.F('age')),
            [self.a3, self.a4], lambda x: x)
        self.assertQuerysetEqual(
            baseqs.filter(birthdate__year__lt=models.F('age')),
            [self.a4], lambda x: x)

    def test_year_lte_sql(self):
        # This test will just check the generated SQL for __lte. This
        # doesn't require running on PostgreSQL and spots the most likely
        # error - not running YearLte SQL at all.
        baseqs = Author.objects.order_by('name')
        self.assertIn(
            '<= (2011 || ', str(baseqs.filter(birthdate__year__lte=2011).query))
        self.assertIn(
            '-12-31', str(baseqs.filter(birthdate__year__lte=2011).query))

    def test_postgres_year_exact(self):
        baseqs = Author.objects.order_by('name')
        self.assertIn(
            '= (2011 || ', str(baseqs.filter(birthdate__year=2011).query))
        self.assertIn(
            '-12-31', str(baseqs.filter(birthdate__year=2011).query))

    def test_custom_implementation_year_exact(self):
        try:
            # Two ways to add a customized implementation for different backends:
            # First is MonkeyPatch of the class.
            def as_custom_sql(self, compiler, connection):
                lhs_sql, lhs_params = self.process_lhs(compiler, connection, self.lhs.lhs)
                rhs_sql, rhs_params = self.process_rhs(compiler, connection)
                params = lhs_params + rhs_params + lhs_params + rhs_params
                return ("%(lhs)s >= str_to_date(concat(%(rhs)s, '-01-01'), '%%%%Y-%%%%m-%%%%d') "
                        "AND %(lhs)s <= str_to_date(concat(%(rhs)s, '-12-31'), '%%%%Y-%%%%m-%%%%d')" %
                        {'lhs': lhs_sql, 'rhs': rhs_sql}, params)
            setattr(YearExact, 'as_' + connection.vendor, as_custom_sql)
            self.assertIn(
                'concat(',
                str(Author.objects.filter(birthdate__year=2012).query))
        finally:
            delattr(YearExact, 'as_' + connection.vendor)
        try:
            # The other way is to subclass the original lookup and register the subclassed
            # lookup instead of the original.
            class CustomYearExact(YearExact):
                # This method should be named "as_mysql" for MySQL, "as_postgresql" for postgres
                # and so on, but as we don't know which DB we are running on, we need to use
                # setattr.
                def as_custom_sql(self, compiler, connection):
                    lhs_sql, lhs_params = self.process_lhs(compiler, connection, self.lhs.lhs)
                    rhs_sql, rhs_params = self.process_rhs(compiler, connection)
                    params = lhs_params + rhs_params + lhs_params + rhs_params
                    return ("%(lhs)s >= str_to_date(CONCAT(%(rhs)s, '-01-01'), '%%%%Y-%%%%m-%%%%d') "
                            "AND %(lhs)s <= str_to_date(CONCAT(%(rhs)s, '-12-31'), '%%%%Y-%%%%m-%%%%d')" %
                            {'lhs': lhs_sql, 'rhs': rhs_sql}, params)
            setattr(CustomYearExact, 'as_' + connection.vendor, CustomYearExact.as_custom_sql)
            YearTransform.register_lookup(CustomYearExact)
            self.assertIn(
                'CONCAT(',
                str(Author.objects.filter(birthdate__year=2012).query))
        finally:
            YearTransform._unregister_lookup(CustomYearExact)
            YearTransform.register_lookup(YearExact)


class TrackCallsYearTransform(YearTransform):
    lookup_name = 'year'
    call_order = []

    def as_sql(self, compiler, connection):
        lhs_sql, params = compiler.compile(self.lhs)
        return connection.ops.date_extract_sql('year', lhs_sql), params

    @property
    def output_field(self):
        return models.IntegerField()

    def get_lookup(self, lookup_name):
        self.call_order.append('lookup')
        return super(TrackCallsYearTransform, self).get_lookup(lookup_name)

    def get_transform(self, lookup_name):
        self.call_order.append('transform')
        return super(TrackCallsYearTransform, self).get_transform(lookup_name)


class LookupTransformCallOrderTests(TestCase):
    def test_call_order(self):
        models.DateField.register_lookup(TrackCallsYearTransform)
        try:
            # junk lookup - tries lookup, then transform, then fails
            with self.assertRaises(FieldError):
                Author.objects.filter(birthdate__year__junk=2012)
            self.assertEqual(TrackCallsYearTransform.call_order,
                             ['lookup', 'transform'])
            TrackCallsYearTransform.call_order = []
            # junk transform - tries transform only, then fails
            with self.assertRaises(FieldError):
                Author.objects.filter(birthdate__year__junk__more_junk=2012)
            self.assertEqual(TrackCallsYearTransform.call_order,
                             ['transform'])
            TrackCallsYearTransform.call_order = []
            # Just getting the year (implied __exact) - lookup only
            Author.objects.filter(birthdate__year=2012)
            self.assertEqual(TrackCallsYearTransform.call_order,
                             ['lookup'])
            TrackCallsYearTransform.call_order = []
            # Just getting the year (explicit __exact) - lookup only
            Author.objects.filter(birthdate__year__exact=2012)
            self.assertEqual(TrackCallsYearTransform.call_order,
                             ['lookup'])

        finally:
            models.DateField._unregister_lookup(TrackCallsYearTransform)


class CustomisedMethodsTests(TestCase):

    def test_overridden_get_lookup(self):
        q = CustomModel.objects.filter(field__lookupfunc_monkeys=3)
        self.assertIn('monkeys()', str(q.query))

    def test_overridden_get_transform(self):
        q = CustomModel.objects.filter(field__transformfunc_banana=3)
        self.assertIn('banana()', str(q.query))

    def test_overridden_get_lookup_chain(self):
        q = CustomModel.objects.filter(field__transformfunc_banana__lookupfunc_elephants=3)
        self.assertIn('elephants()', str(q.query))

    def test_overridden_get_transform_chain(self):
        q = CustomModel.objects.filter(field__transformfunc_banana__transformfunc_pear=3)
        self.assertIn('pear()', str(q.query))
