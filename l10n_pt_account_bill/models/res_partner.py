# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, exceptions, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    bill_id = fields.Char("BILL ID", copy=False, readonly=True)

    def _bill_find_existing_contact(self, vals):
        """O BILL respondeu "já existe" (231/253): localizar o contacto.

        Primeiro por `codigo`; se vier vazio — típico de clientes criados no
        Odoo 14, cujo `res.partner.bill_id` NÃO foi migrado e cujo código no
        BILL não bate com o "A{id}" novo — procura-se por **NIF** (o duplicado é
        detetado pelo NIF). Só o param `pesquisa[nif]` filtra mesmo por NIF; o
        `numero_contribuinte`/`contribuinte` são ignorados e devolvem lixo."""
        self.ensure_one()
        BILL = self.env["account.bill"]
        company = self.company_id or self.env.company
        for params in (
            {"pesquisa[codigo]": vals.get("codigo")},
            {"pesquisa[nif]": vals.get("nif")},
        ):
            if not list(params.values())[0]:
                continue
            data = (BILL.call(company, "contatos", "GET", payload=params).json()
                    or {}).get("data") or []
            if data:
                return data[0]
        raise exceptions.UserError(_(
            "O cliente '%s' (NIF %s) já existe no BILL mas não foi possível "
            "localizá-lo por código nem por NIF. Defina o BILL ID manualmente "
            "no separador de contabilidade do cliente."
        ) % (self.name, self.vat or "—"))

    def _prepare_bill_vals(self):
        self.ensure_one()
        vals = {
            "nome": self.name,
            "pais": self.country_id.code if self.country_id else "PT",
            "codigo": "A{}".format(self.ref or self.id),
            "nif": self.vat,
            "email": self.email,
            "morada": ", ".join(filter(None, [self.street, self.street2])),
            "codigo_postal": self.zip,
            "cidade": self.city,
            "telefone_contato": self.phone, 
        }
        return {k: v for k, v in vals.items() if v}

    def set_bill_contact(self):
        self.ensure_one()
        self.vat and self._check_vat()  # Double check VAT is right (Odoo 20: check_vat -> _check_vat)
        BILL = self.env["account.bill"]
        company = self.company_id or self.env.company
        doctype = "contatos"
        vals = self._prepare_bill_vals()
        invx_id_to_update = self.bill_id
        if not invx_id_to_update:
            # Create: POST /clients.json
            response = BILL.call(
                company,
                doctype,
                "POST",
                payload=vals,
                raise_errors=False,
            )
            if response.text == '{"error":["231"]}' or response.text == '{"error":["253"]}':  # Oh, it already exists!
                response = BILL.call(
                    company,
                    doctype,
                    "GET",
                    payload={"pesquisa[codigo]": vals["codigo"]},
                )
                values = self._bill_find_existing_contact(vals)
                invx_id_to_update = values.get("id")  # Update is needed!
            else:
                values = response.json()

            self.bill_id = values.get("id")

        if invx_id_to_update:
            # Update: PUT /clients/$(client-id).json
            try:
                response = BILL.call(
                    company,
                    "{}/{}".format(doctype, self.bill_id),
                    "PATCH",
                    payload=vals,
                    raise_errors=True,
                )
            except Exception as e:
                # Create: POST /clients.json
                vals["codigo"]  = "B{}".format(self.ref or self.id)
                response = BILL.call(
                    company,
                    doctype,
                    "POST",
                    payload=vals,
                    raise_errors=False,
                )
                if response.text == '{"error":["231"]}' or response.text == '{"error":["253"]}':  # Oh, it already exists!
                    response = BILL.call(
                        company,
                        doctype,
                        "GET",
                        payload={"pesquisa[codigo]": vals["codigo"]},
                    )
                    values = self._bill_find_existing_contact(vals)
                    invx_id_to_update = values.get("id")  # Update is needed!
                else:
                    values = response.json()
                
                self.bill_id = values.get("id")
        
        val_final={} 
        for key,val in vals.items():
            val_final['contato[{}]'.format(key)] = val
        
        return val_final#{"nome": vals["nome"], "codigo": vals["codigo"]}
