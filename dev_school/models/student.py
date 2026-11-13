from odoo import api, fields, models


class SchoolStudent(models.Model):
    _name = "dev.school.student"
    _description = "School Student"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(default=True)
    student_number = fields.Integer(string="Student Number")
    age = fields.Integer(string="Age")
    admission_date = fields.Date(string="Admission Date", default=fields.Date.context_today)
    last_attended = fields.Datetime(string="Last Attended")
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    biography = fields.Html(string="Biography")
    gender = fields.Selection(
        [("female", "Female"), ("male", "Male"), ("other", "Other")],
        string="Gender",
    )
    tuition = fields.Monetary(string="Tuition", currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency", string="Currency", default=lambda self: self.env.company.currency_id
    )
    gpa = fields.Float(string="GPA", digits=(3, 2))
    photo = fields.Image(string="Photo", max_width=1024, max_height=1024)
    attachment = fields.Binary(string="Attachment", attachment=True)
    attachment_filename = fields.Char(string="Attachment Filename")
  