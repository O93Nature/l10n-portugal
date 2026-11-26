# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import uuid
import json
import base64
from datetime import datetime
from odoo import _, api, exceptions, fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"


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

    def _prepare_receipt_lines(self):
        items = {}
        for i, line in enumerate(self.reconciled_invoice_ids):

            items.update(
                {
                    "documentos[{}][documento_id]".format(i): int(line.bill_id),
                    "documentos[{}][total]".format(i): self.amount,
                    "documentos[{}][total_desconto]".format(i): 0,
                }
            )

        return items

    def _get_bill_partner(self):
        # Hook to customize the "client" values to use
        return self.commercial_partner_id

    def _prepare_receipt_vals(self):
        self.ensure_one()

        customer = self._get_bill_partner()
        if not customer.bill_id:
            customer.set_bill_contact()
        items = self._prepare_receipt_lines()
        # Campos do funeral (defunct, funeral_datetime) vem do funeral_manager
        funeral_data = self.reconciled_invoice_ids.process
        receipt_data = {
            "tipo_documento_id": 28,
            "contato_id": int(customer.bill_id),
            "observacoes": "Despesas referentes ao funeral do(a) Exmo.(a) Senhor(a) %s.\n Processo: %s \n Data do Funeral: %s"%(funeral_data.defunct.name, funeral_data.process, funeral_data.funeral_datetime.strftime('%d-%m-%Y') if funeral_data.funeral_datetime else '') if "defunct" in funeral_data._fields else "",
        }
        
        receipt_data.update(items)
        #receipt_data.update({"terminado": 1})

        return receipt_data

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

    def _get_existing_bill_receipt(self):
        """Se a(s) fatura(s) reconciliada(s) já tiverem recibo no BILL, devolve
        {'id':..., 'invoice_number':...} do recibo existente.

        Cenário: uma tentativa anterior de "Criar Pagamento" criou o recibo no
        BILL (POST com sucesso) mas a transação do Odoo fez rollback a seguir
        (o POST ao BILL não é transacional). O recibo fica órfão no BILL e, ao
        repetir, o BILL recusa um 2.º recibo sobre o mesmo documento com
        "documento_id is invalid". Nesse caso adotamos o recibo existente em
        vez de criar outro."""
        BILL = self.env["account.bill"]
        inv = self.reconciled_invoice_ids[:1]
        if not inv or not inv.bill_id:
            return False
        try:
            doc = BILL.call(
                inv.company_id, "documentos/{}".format(int(inv.bill_id)), "GET"
            ).json()
        except Exception:  # noqa: BLE001 - na dúvida segue o fluxo normal
            return False
        for rec in doc.get("pago_por") or []:
            recdoc = rec.get("documento") or {}
            rec_id = rec.get("documento_id") or recdoc.get("id")
            if rec_id:
                return {
                    "id": rec_id,
                    "invoice_number": recdoc.get("invoice_number"),
                }
        return False

    def action_create_bill_receipt(self):
        BILL = self.env["account.bill"]

        values = self._get_existing_bill_receipt()
        if not values:
            payload = self._prepare_receipt_vals()
            values = BILL.call(
                self.company_id, "recibos", "POST", payload=payload
            ).json()
        if not values:
            raise exceptions.UserError(
                _("Something went wrong: the BILL response looks empty.")
            )
        # No Odoo 20 o account.payment NÃO delega para o move (não há _inherits):
        # o bill_id/nome do recibo têm de ficar no PRÓPRIO lançamento (move_id),
        # que é onde a deteção (has_bill_receipt) e o wizard de anulação os leem.
        # O bill_id é o que LIGA ao recibo (deteção/anulação) -> grava-se sempre.
        self.move_id.bill_id = values.get("id")
        # O nome do recibo (RC BILL/NNN) pode colidir com outro move: a sequência
        # de pagamentos do Odoo e a sequência de recibos do BILL são independentes
        # e podem divergir (ex. um pagamento de banco apanhou o nome RC BILL/NNN).
        # Só renomeamos se o nome estiver livre; senão mantemos o nome atual e o
        # vínculo fica garantido pelo bill_id.
        new_name = values.get("invoice_number")
        if new_name and new_name != self.move_id.name:
            clash = self.env["account.move"].search_count([
                ("name", "=", new_name),
                ("company_id", "=", self.move_id.company_id.id),
                ("id", "!=", self.move_id.id),
            ])
            if not clash:
                self.move_id.name = new_name
        #self._update_bill_status()
        receipt_details = BILL.call(self.company_id, "documentos/{}".format(values.get("id")), "GET").json()
        token_download = receipt_details['token_download']

        response = BILL.call(self.company_id, "documentos/download/{}/{}".format(values.get("id"), token_download), "GET")
        content = response.content.replace(b'/JS', b'//JS')

        attachment = self.env['ir.attachment'].create({
            'name': '{}.pdf'.format(values.get('invoice_number')),
            'type': 'binary',
            'raw': content,
            'res_model': 'account.payment',
            'res_id': self.id,
            'mimetype': 'application/pdf'
        })
        
        attachment = self.env['ir.attachment'].create({
            'name': '{}.pdf'.format(values.get('invoice_number')),
            'type': 'binary',
            'raw': content,
            'res_model': 'account.move',
            'res_id': self.reconciled_invoice_ids.id,
            'mimetype': 'application/pdf'
        })

        self.with_context(no_new_invoice=True).message_post(attachment_ids=[attachment.id])

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

    # No Odoo 20 o bill_id/nome do recibo vivem no MOVE (pay.move_id), não no
    # account.payment (ao contrário do 14, que delega por _inherits).
    fm_has_bill_receipt = fields.Boolean(
        string="Recibo BILL por anular",
        compute="_compute_fm_has_bill_receipt")

    @api.depends("move_id.bill_id", "move_id.state")
    def _compute_fm_has_bill_receipt(self):
        for pay in self:
            pay.fm_has_bill_receipt = bool(
                pay.move_id.bill_id and pay.move_id.state != "cancel")

    def action_open_receipt_cancel_wizard(self):
        """Abre o diálogo para anular o recibo (RC …) no BILL."""
        return {
            "type": "ir.actions.act_window",
            "name": _("Anular Recibo"),
            "res_model": "account.payment.receipt.cancel.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_ids": self.ids, "active_model": "account.payment"},
        }


class AccountPaymentReceiptCancelWizard(models.TransientModel):
    _name = "account.payment.receipt.cancel.wizard"
    _description = "Anular Recibo (BILL)"

    reason = fields.Char(string="Motivo da Anulação", required=True, size=200)

    def action_confirm_cancel(self):
        """Anula o recibo no BILL (certificado) e desfaz o pagamento: anexa o
        PDF anulado (ao pagamento e à fatura), desreconcilia (a fatura volta a
        "não pago") e cancela o lançamento do recibo. Espelha o wizard das
        faturas, adaptado à API do Odoo 20 (bill_id no move, account_type,
        raw, remove_move_reconcile)."""
        active_ids = self.env.context.get("active_ids")
        if not active_ids:
            raise exceptions.UserError(_("Nenhum recibo selecionado."))
        payments = self.env["account.payment"].browse(active_ids)
        BILL = self.env["account.bill"]
        for pay in payments:
            move = pay.move_id
            if move.state == "cancel":
                continue
            bid = move.bill_id
            if not bid:
                raise exceptions.UserError(
                    _("Este pagamento não tem recibo BILL para anular.")
                )
            rc_name = move.name
            # 1) Anular no BILL (mesmo endpoint das faturas).
            payload = {
                "id": bid,
                "motivo_anular": self.reason,
                "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                BILL.call(pay.company_id, "documentos", "PATCH", payload=payload).json()
            except Exception:
                raise exceptions.UserError(
                    _("Não foi possível anular o recibo %s no BILL.") % rc_name
                )
            # Anexar o PDF anulado ao pagamento E à(s) fatura(s) — antes da
            # desreconciliação, que esvazia reconciled_invoice_ids.
            try:
                doc = BILL.call(pay.company_id, "documentos/{}".format(bid), "GET")
                data = json.loads(doc.content)
                if data.get("estado") == "A":
                    token = data.get("token_download")
                    resp = BILL.call(
                        pay.company_id,
                        "documentos/download/{}/{}".format(bid, token),
                        "GET",
                        payload=payload,
                    )
                    content = resp.content.replace(b"/JS", b"//JS")
                    targets = [("account.payment", pay.id)]
                    targets += [
                        ("account.move", inv.id)
                        for inv in pay.reconciled_invoice_ids
                    ]
                    for model, rid in targets:
                        self.env["ir.attachment"].create(
                            {
                                "name": "{}_Anulado.pdf".format(rc_name),
                                "type": "binary",
                                "raw": content,
                                "res_model": model,
                                "res_id": rid,
                                "mimetype": "application/pdf",
                            }
                        )
            except Exception:
                pass
            # 2) Desreconciliar + cancelar o lançamento do recibo.
            recon_lines = move.line_ids.filtered(
                lambda l: l.account_type in ("asset_receivable", "liability_payable")
            )
            recon_lines.remove_move_reconcile()
            move.write({"state": "cancel"})
            pay.message_post(
                body=_("Recibo %s anulado no BILL. Motivo: %s")
                % (rc_name, self.reason)
            )
        return {"type": "ir.actions.act_window_close"}
