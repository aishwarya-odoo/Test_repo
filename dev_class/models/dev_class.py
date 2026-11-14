from odoo import fields, models


class SchoolClass(models.Model):
    _name = "dev.school.class"
    _description = "School Class"
    _order = "name"

    name = fields.Char(string="Class Name", required=True)
    active = fields.Boolean(default=True)
    class_code = fields.Char(string="Class Code")
    grade_level = fields.Selection(
        [
            ("1", "Grade 1"),
            ("2", "Grade 2"),
            ("3", "Grade 3"),
            ("4", "Grade 4"),
            ("5", "Grade 5"),
            ("6", "Grade 6"),
            ("7", "Grade 7"),
            ("8", "Grade 8"),
            ("9", "Grade 9"),
            ("10", "Grade 10"),
            ("11", "Grade 11"),
            ("12", "Grade 12"),
        ],
        string="Grade Level",
    )
    room = fields.Char(string="Room")
    capacity = fields.Integer(string="Capacity", default=30)
    start_date = fields.Date(string="Start Date")
    description = fields.Text(string="Description")

    student_ids = fields.One2many(
        "dev.school.student", "class_id", string="Students"
    )
    teacher_id = fields.Many2one("res.users", string="Teacher")
