# Pendiente

1. Automatizar valores actuales de la cartera.
2. Que se pueda usar desde desktop.
3. Que la cuenta de trade republic se registre como fijo. Que se tengan en cuenta los retornos de dicha cuenta y del 1% de la tarjeta de crédito. Esto serán asset clases.
4. Que se hagan filtros del tipo de asset a partir del tipo de cuenta.
5. Tratar ganancias realizadas/por realizar.

# Hecho

- Página `/sync`: Revolut (pegando el cURL de app.revolut.com), Trade Republic
  (pytr), Bankinter e ING (Enable Banking, PSD2) con un clic; Edenred y Mintos
  se suben desde la misma página y se importan con los ajustes guardados.
  B100, Iberia Cards y MyInvestor quedan archivados (solo histórico).

- Añadir inversiones desde un fichero (Trade Republic CSV + Mintos xlsx).
- Importador de Mintos: el extracto se agrega en el holding "Mintos Lending"
  (depósitos = base de coste, valor total → evolución que registra la ganancia).
  Idempotente al reimportar. Ruta `/import/mintos`.
- Importador de Edenred: soporta el segundo formato de exportación ("Sheet1")
  además del de "Movimientos"; el formato antiguo rellena el histórico previo sin
  duplicar el periodo que ya cubre el formato con comercios. Separa el gasto
  automáticamente: tarjeta Restaurante → categoría "Restaurantes", tarjeta
  Movilidad → categoría "Transporte" (la cuenta destino se recuerda por producto).
