# Order Event Processing (task2)

A consumer that receives order events from a message stream, checks and reshapes them, keeps the orders it has accepted, and hands each one on to a downstream system.

## Language

**Order placed event**:
The announcement that a customer has placed a new order, carrying the complete order. The only kind of order event this context consumes.
_Avoid_: order message, order created event

**Order**:
A customer's purchase of one or more products in a single currency, identified by its order id. The customer is referenced by id only; no personal details travel with the order.
_Avoid_: purchase, transaction

**Order line**:
One product in an order: its SKU, the quantity and the unit price. A SKU appears at most once per order.
_Avoid_: item, position

**Order total**:
The amount the producer states for the whole order. It must equal the sum of quantity × unit price over the order lines.
_Avoid_: sum, grand total

**Fulfilment service**:
The downstream system that ships accepted orders. It keeps its own record of each order, the fulfilment order, under its own id.
_Avoid_: downstream API, target system

**Handover**:
Passing an accepted order to the fulfilment service. A handover is complete once the fulfilment service has confirmed it with its fulfilment order id.
_Avoid_: forwarding, sync, push

**Failed handover**:
A handover the consumer has given up on, because it ran out of retries or because the fulfilment service refused the order. The order stays accepted, and the handover can still be completed later.
_Avoid_: lost order, rejected order

**Invalid event**:
An event that can never be processed as sent: unreadable, not a well-formed event, of an unsupported type or version, breaking the schema, or breaking a business rule such as a wrong order total. Being invalid is permanent; retrying cannot change it.
_Avoid_: bad message, poison pill (a crash-causing message, which is a different problem)

**Event id**:
The identity of one order placed event. A redelivery of the same event keeps its event id; a new event about the same order gets a new one.
_Avoid_: message id, order id (the order has its own identity)

**Duplicate event**:
An order placed event for an order that has already been accepted. It arrives either as a redelivery or as a re-emission.
_Avoid_: double, repeat

**Conflicting event**:
An order placed event for an order that has already been accepted, but describing it differently (other lines, another total). The first accepted version stands; the conflicting event is kept aside for a person to look at.
_Avoid_: update, correction (there are no order updates in this context)

**Redelivery**:
The same event, with the same event id, arriving again, for example after the consumer restarted before recording its progress.
_Avoid_: retry (a retry is the consumer trying again, not the event arriving again)

**Re-emission**:
The producer sending an order again as a new event, with a new event id.
_Avoid_: resend, replay (replay means feeding dead letters back in)
