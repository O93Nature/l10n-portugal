# Recibo BILL idempotente no "Registar Pagamento"

## Sintoma
Ao "Criar Pagamento" (wizard Registar Pagamento) de uma fatura de cliente,
a API do BILL devolve:

```
400 Bad Request: {'error': ['The selected documentos.0.documento_id is invalid.']}
```

e o pagamento **não fica registado** no Odoo.

## Causa raiz
`account.move.action_create_payments` → por cada pagamento inbound chama
`account.payment.action_create_bill_receipt`, que faz **POST /recibos** ao BILL
com `documentos[0][documento_id] = fatura.bill_id`.

O POST ao BILL **não é transacional** com o Odoo. Se uma tentativa anterior:
1. criou o recibo no BILL com sucesso (ex. RC BILL/193), mas
2. a transação do Odoo fez **rollback** logo a seguir (falha num passo
   pós-POST, noutro registo, timeout, etc.),

então no BILL a fatura fica **paga** (`divida.total_divida = 0`,
`pago_por = [recibo]`) mas no Odoo o pagamento desaparece (rollback) e o
`move_id.bill_id` fica vazio.

Ao repetir, o código tenta criar um **2.º** recibo sobre o mesmo documento e o
BILL recusa (um documento liquidado não aceita novo recibo) → *"documento_id is
invalid"*. A fatura fica impossível de pagar pelo fluxo normal.

## Diagnóstico (como confirmar)
`GET documentos/<bill_id>` e inspecionar:
- `divida.total_divida == "0.00"` e `divida.data_finalizado` preenchido → já liquidado;
- `pago_por` → lista com o recibo que o pagou (tem `documento.invoice_number`,
  ex. `RC BILL/193`, e `documento_id` do recibo).

## Correção (idempotência)
`models/account_payment.py`:
- Novo `_get_existing_bill_receipt()`: faz GET ao documento da fatura
  reconciliada e, se `pago_por` tiver recibo, devolve `{id, invoice_number}`.
- `action_create_bill_receipt()`: **antes** de fazer POST, chama
  `_get_existing_bill_receipt()`. Se já existe recibo, **adota-o** (usa esse
  `id`/`invoice_number`, liga ao `move_id`, descarrega o PDF e anexa) em vez de
  criar outro. Só faz POST quando não há recibo.

Resultado: repetir "Criar Pagamento" passa a adotar o recibo órfão e o
pagamento fica registado e reconciliado no Odoo. Sem `-u` (só Python, basta
reiniciar o serviço).

## Pendente / melhoria futura
A causa de fundo é a não-transacionalidade: idealmente o POST ao BILL devia ser
o **último** passo (ou o PDF/anexos deviam ser tolerantes a falha) para não
deixar recibos órfãos. A idempotência acima resolve o impacto, mas convém rever
a ordem das operações em `action_create_bill_receipt`.

## 2.º obstáculo: colisão de nome (RC BILL/NNN)
Depois da idempotência, o passo `move_id.name = recibo_number` rebentava com
*"Já existe um lançamento com o mesmo nome"*. Causa: o diário **Banco** numera
**todos** os pagamentos (inbound E outbound) com o prefixo `RC BILL/` (série
semeada nos dados, não no código). Um pagamento de **fornecedor** (outbound,
ex. pay 208 = BILL/2026/11/0020) apanhou `RC BILL/193`, colidindo com o número
que o BILL deu ao recibo de cliente da FT BILL/225.

**Mitigação aplicada:** em `action_create_bill_receipt`, o `bill_id` (vínculo
ao recibo, usado pela deteção/anulação) grava-se sempre; o **nome** só é
aplicado se estiver livre. A FT BILL/225 ficou paga com o pagamento nomeado
`RC BILL/194` e `bill_id=4357724` (recibo `RC BILL/193` no BILL).

**Causa de fundo (pendente):** os pagamentos de **fornecedor/outbound** não
deviam consumir a série `RC BILL/` (reservada a recibos certificados de
cliente). Falta dar-lhes série própria (ex. `PBNK1/`), senão a numeração
Odoo↔BILL continua a divergir e a colidir.
