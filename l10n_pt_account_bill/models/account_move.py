# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import uuid
import base64
import json
from odoo import _, api, exceptions, fields, models
from datetime import datetime
from odoo.exceptions import UserError

class AccountMove(models.Model):
    _inherit = "account.move"

    process = fields.Many2one('sale.order', string='Processo')
    defunct = fields.Char(string='Falecido')
    
    @api.depends("restrict_mode_hash_table", "state")
    def _compute_show_reset_to_draft_button(self):
        super()._compute_show_reset_to_draft_button()
        # BILL generated invoices can't be set to Draft
        self.filtered("bill_id").write({"show_reset_to_draft_button": False})

    @api.depends("move_type", "journal_id.use_bill")
    def _compute_can_bill(self):
        for invoice in self:
            invoice.can_bill = (
                invoice.journal_id.use_bill and invoice.is_sale_document()
            )

    @api.depends("can_bill", "company_id.bill_template_id")
    def _compute_can_bill_email(self):
        for invoice in self:
            invoice.can_bill_email = (
                invoice.can_bill
                and invoice.company_id.bill_template_id
            )

    @api.depends("move_type", "journal_id", "partner_shipping_id")
    def _compute_bill_doc_type(self):
        """
        The type of document to create: invoices, invoice_receipts,
        simplified_invoices, vat_moss_invoices, credit_notes or debit_notes.
        """
        invoices = self.filtered("journal_id.use_bill")
        for invoice in invoices:
            doctype = invoice.journal_id.bill_doc_type
            if not doctype or doctype == "none":
                res = None
            elif invoice.move_type == "out_refund":
                res = "credit_note"
            else:
                res = doctype
            invoice.bill_doc_type = res

    journal_type = fields.Selection(
        related="journal_id.type", string="Journal Type", readonly=True
    )
    bill_id = fields.Char("BILL ID", copy=False, readonly=True)
    bill_permalink = fields.Char(
        "BILL Doc Link", copy=False
    )
    can_bill = fields.Boolean(compute="_compute_can_bill")
    can_bill_email = fields.Boolean(compute="_compute_can_bill_email")

    bill_doc_type = fields.Selection(
        [
            ("invoice", "Invoice"),
            ("invoice_receipt", "Invoice and Receipt"),
            ("simplified_invoice", "Simplified Invoice"),
            ("vat_moss_invoice", "Europe VAT MOSS Invoice"),
            ("vat_moss_credit_note", "Europe VAT MOSS Credit Note"),
            ("debit_note", "Debit Note"),
            ("credit_note", "Credit Note"),
        ],
        compute="_compute_bill_doc_type",
        store=True,
        readonly=False,
        copy=False,
        help="Select the type of legal invoice document"
        " to be created by BILL."
        " If unset, BILL will not be used.",
    )

    cancel_reason = fields.Char("Motivo da Anulação", readonly=True)

    @api.constrains("journal_id", "company_id")
    def _check_bill_doctype_config(self):
        """
        Ensure Journal configuration was not forgotten.
        """
        sale_invoices = self.filtered(lambda x: x.journal_id.type == "sale")
        for invoice in sale_invoices:
            journal_doctype = invoice.journal_id.bill_doc_type
            has_bill = invoice.company_id.has_bill
            if not journal_doctype and has_bill:
                raise exceptions.UserError(
                    _(
                        "Journal %s is missing the BILL"
                        " document type configuration!"
                    )
                    % invoice.journal_id.display_name
                )

    @api.model
    def _get_bill_prefix(self, doctype):
        return {
            "invoice": "FT",
            "invoice_receipt": "FR",
            "simplified_invoice": "FS",
            "vat_moss_invoice": "FVM",
            # vat_moss_credit_note does not have a prefix!
            "credit_note": "NC",
            "debit_note": "ND",
        }.get(doctype)

    def _prepare_bill_lines(self):
        # FIXME: set user lang, based on country?
        lines = self.invoice_line_ids.filtered(
            lambda l: l.display_type not in ("line_section", "line_note")
        )
        # Ensure Taxes are created on BILL
        #lines.mapped("tax_ids").action_bill_tax_create()
        items = {}
        for i, line in enumerate(lines):
            tax = line.tax_ids[:1]
            # If not tax set, force zero VAT
            tax_detail = {"name": tax.name or "IVA0", "value": tax.amount or 0.0}
            items.update(
                {
                    "produtos[{}][codigo]".format(i): line.product_id.default_code,
                    "produtos[{}][nome]".format(i): line.product_id.name,
                    "produtos[{}][quantidade]".format(i): line.quantity,
                    "produtos[{}][preco_unitario]".format(i): line.price_unit,
                    "produtos[{}][unidade_medida_id]".format(i): 20842,
                    "produtos[{}][imposto]".format(i): tax.amount,
                    "produtos[{}][isencao]".format(i): 7,
                    "produtos[{}][desconto_1]".format(i): line.discount,

                    #"produtos[{}][motivo_isencao_id]".format(i): 1,
                    #"produtos[{}][item_id]".format(i): 12244,
                    #'nome": line._get_bill_descr(),
                    #"imposto": tax_detail,
                    #"desconto_1": line.discount
                }
            )
            if self.bill_doc_type == 'credit_note':
                items.update({
                    #"produtos[{}][lancamento_pai_id]".format(i): int(self.reversed_entry_id.bill_id)
                    "produtos[{}][referencia_manual]".format(i): self.reversed_entry_id.display_name
                })
        return items

    def _get_bill_partner(self):
        # Hook to customize the "client" values to use
        return self.commercial_partner_id

    def _prepare_bill_vals(self):
        self.ensure_one()
        if not self.invoice_date and self.invoice_date_due:
            raise exceptions.UserError(
                _("Kindly add the invoice date and invoice due date.")
            )
        customer = self._get_bill_partner()
        customer_vals = customer.set_bill_contact()
        items = self._prepare_bill_lines()
        proprietary_uid = "ODOO" + str(uuid.uuid4()).replace("-", "")

        tipificacao = self._get_bill_prefix(self.bill_doc_type)

        funeral_data = self.env['sale.order'].search([('name', '=', self.invoice_origin)], limit=1)
        # Campos do funeral (defunct, funeral_datetime) vem do funeral_manager
        is_funeral = "defunct" in funeral_data._fields

        invoice_data = {
            #"data": self.invoice_date.strftime("%Y-%m-%d %H:%m:%s"),
            #"prazo_vencimento": self.invoice_date_due.strftime("%Y-%m-%d %H:%m:%s"),
            #"reference": self.ref or "",
            "tipificacao": "{}".format(tipificacao),
            #"tipo_documento_id": 1,
            #"contato_id": customer_vals['codigo'],
            #"contato[nome]": customer_vals['nome'],
            "observacoes": "Despesas referentes ao funeral do(a) Exmo.(a) Senhor(a) %s.\n Processo: %s \n Data do Funeral: %s"%(funeral_data.defunct.name, funeral_data.process, funeral_data.funeral_datetime.strftime('%d-%m-%Y') if funeral_data.funeral_datetime else '') if is_funeral else ""
            #"proprietary_uid": proprietary_uid,
        }

        if self.bill_doc_type in ("credit_note", "debit_note"):
            invoice_data.update({"motivo": self.ref})
        
        invoice_data.update(customer_vals)
        invoice_data.update(items)
        invoice_data.update({"terminado": 1})
        '''exempt_code = self.l10npt_vat_exempt_reason.code
        if exempt_code:
            invoice_data["invoice"]["tax_exemption"] = exempt_code'''
        '''if self.company_id.currency_id != self.currency_id:
            currency_rate = self.env["res.currency"]._get_conversion_rate(
                self.company_id.currency_id,
                self.currency_id,
                self.company_id,
                self.invoice_date,
            )
            invoice_data["invoice"].update(
                {"currency_code": self.currency_id.name, "rate": str(currency_rate)}
            )'''
        #doctype = self.bill_doc_type
        '''if doctype in ("credit_note", "debit_note"):
            owner_invoice_num = self.reversed_entry_id.bill_id
            if owner_invoice_num:
                invoice_data["invoice_owner_invoice_id"] = owner_invoice_num'''
        return invoice_data

    def _update_bill_status(self):
        inv_xpress_link_name = _("View Document")
        inv_xpress_link = (
            "<a class='btn btn-info mr-2' target='new' href={}>{}</a>"
        ).format(self.bill_permalink, inv_xpress_link_name)
        msg = _(
            "BILL record has been created for this invoice:"
            "<ul><li>BILL Id: {inv_xpress_id}</li>"
            "<li>{inv_xpress_link}</li></ul>"
        ).format(inv_xpress_id=self.bill_id, inv_xpress_link=inv_xpress_link)
        self.message_post(body=msg)

    def action_create_bill_invoice(self):
        BILL = self.env["account.bill"]
        for invoice in self.filtered("can_bill"):
            doctype = invoice.bill_doc_type
            if not doctype:
                raise exceptions.UserError(
                    _("Invoice is missing the BILL document type!")
                )
            payload = invoice._prepare_bill_vals()
            values = BILL.call(invoice.company_id, "documentos", "POST", payload=payload).json()
            if not values:
                raise exceptions.UserError(
                    _("Something went wrong: the BILL response looks empty.")
                )
            invoice.bill_id = values.get("id")

            #values1 = response1.get(doctype)
            '''seqnum = values1 and values1.get("inverted_sequence_number")
            if not seqnum:
                raise exceptions.UserError(
                    _(
                        "Something went wrong: the BILL response"
                        " is missing a sequence number."
                    )
                )'''
            #prefix = self._get_bill_prefix(doctype)
            invx_number = values.get("invoice_number")
            if self.invoice_origin:
                invoice.payment_reference = self.invoice_origin
                funeral_data = self.env['sale.order'].search([('name', '=', self.invoice_origin)], limit=1)
                invoice.process = funeral_data
                if "defunct" in funeral_data._fields:
                    invoice.defunct = funeral_data.defunct.name
            
            invoice.name = invx_number
            invoice._update_bill_status()

            response = BILL.call(invoice.company_id, "documentos/download/{}/{}".format(invoice.bill_id, values.get("token_download")), "GET", payload=payload)
            invoice.bill_permalink = "https://app.bill.pt/documentos/download/{}/{}".format(invoice.bill_id, values.get("token_download"))

            content = response.content.replace(b'/JS', b'//JS')

            attachment = self.env['ir.attachment'].create({
                'name': '{}.pdf'.format(invx_number),
                'type': 'binary',
                'raw': content,
                'res_model': 'account.move',
                'res_id': self.id,
                'mimetype': 'application/pdf'
            })

            

            invoice.with_context(no_new_invoice=True).message_post(attachment_ids=[attachment.id])

    def _prepare_bill_email_vals(self, ignore_no_config=False):
        self.ensure_one()
        template_id = self.company_id.bill_template_id
        values = template_id.generate_email(
            self.id, ["subject", "body_html", "email_to", "email_cc"]
        )
        if not template_id and not ignore_no_config:
            raise exceptions.UserError(
                _(
                    "Please configure the BILL email template"
                    " at Settings > General Setting, BILL section"
                )
            )
        if not values.get("email_to") and not ignore_no_config:
            raise exceptions.UserError(_("No address to send invoice email to."))
        email_data = None
        if template_id and values["email_to"]:
            email_data = {
                "message": {
                    "client": {"email": values["email_to"], "save": "0"},
                    "cc": values["email_cc"],
                    "subject": values["subject"],
                    "body": values["body_html"],
                }
            }
        return email_data

    def action_send_bill_email(self, ignore_no_config=False):
        BILL = self.env["account.bill"]
        for invoice in self.filtered("can_bill_email"):
            if not invoice.bill_id:
                raise exceptions.UserError(
                    _("Invoice %s is not registered in BILL yet.")
                    % invoice.name
                )
            doctype = invoice.bill_doc_type
            endpoint = "{}s/{}/email-document.json".format(
                doctype, invoice.bill_id
            )
            payload = invoice._prepare_bill_email_vals(ignore_no_config)
            if payload:
                BILL.call(invoice.company_id, endpoint, "PUT", payload=payload)
                msg = _(
                    "Email sent by BILL:<ul><li>To: {}</li><li>Cc: {}</li></ul>"
                ).format(
                    payload["message"]["client"]["email"],
                    payload["message"]["cc"] or _("None"),
                )
                invoice.message_post(body=msg)

    def _post(self, soft=False):
        res = super()._post(soft=soft)
        for invoice in self:
            if not invoice.bill_id:
                invoice._check_bill_doctype_config()
                invoice.action_create_bill_invoice()
                invoice.action_send_bill_email(ignore_no_config=True)
        return res

class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def action_create_payments(self):
        payments = self._create_payments()
        
        if not payments['payment_state']:
            payments.action_create_bill_receipt()

        if self.env.context.get('dont_redirect_to_payments'):
            return True

        action = {
            'name': _('Payments'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'context': {'create': False},
        }
        if len(payments) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': payments.id,
            })
        else:
            action.update({
                'view_mode': 'list,form',
                'domain': [('id', 'in', payments.ids)],
            })
        return action

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        res['communication'] = ''
        return res

class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _get_bill_descr(self):
        """
        Remove Odoo product code from description,
        since it is already presented in a the Code column
        """
        res = self.name
        ref = self.product_id.default_code
        prefix = "[%s] " % ref
        if ref and self.name.startswith(prefix):
            res = self.name[len(prefix) :]
        return res

class AccountInvoiceCancelWizard(models.TransientModel):
    _name = 'account.invoice.cancel.wizard'
    _description = 'Cancel Invoice with Reason'

    reason = fields.Char(
        string="Motivo da Anulação",
        required=True,
        size=200
    )

    def action_cancel_invoice(self):
        for move in self:
            if move.state == 'posted':
                move.state = 'cancel'



    def action_confirm_cancel(self):
        active_ids = self.env.context.get('active_ids')
        if not active_ids:
            raise UserError(_("Nenhuma fatura selecionada."))

        invoices = self.env['account.move'].browse(active_ids)
        for move in invoices:
            if move.state == 'cancel':
                continue
            if move.state == 'posted':
                BILL = self.env["account.bill"]

                try:
                    payload = { 
                                "id": move.bill_id,
                                "motivo_anular": self.reason,
                                "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                }

                    BILL.call(move.company_id, "documentos", "PATCH", payload=payload).json()

                    move.write({
                        'state': 'cancel',
                        'cancel_reason': self.reason,
                    })

                    doc_details = BILL.call(move.company_id, "documentos/{}".format(move.bill_id), "GET")
                    
                    if json.loads(doc_details.content).get('estado') == 'A':
                        token_download = json.loads(doc_details.content).get('token_download')

                        response = BILL.call(move.company_id, "documentos/download/{}/{}".format(move.bill_id, token_download), "GET", payload=payload)
                        content = response.content.replace(b'/JS', b'//JS')

                        self.env['ir.attachment'].create({
                            'name': '{}_Anulada.pdf'.format(move.name),
                            'type': 'binary',
                            'raw': content,
                            'res_model': 'account.move',
                            'res_id': move.id,
                            'mimetype': 'application/pdf'
                        })
                except:
                    raise UserError(_("Não é possível anular esta fatura."))

