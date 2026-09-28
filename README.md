# Calculadora de Flete

Calculadora web para viajes de camión de carga. Se usa desde la PC o el celular.

A partir de los datos de la factura y del viaje calcula:

- Total de la factura (toneladas × precio por tonelada)
- Pago al chofer (porcentaje sobre el total, 18 % por defecto)
- Costo de combustible (litros × precio del gasoil)
- Total de gastos y ganancia neta
- Margen, ganancia y gasoil por tonelada, y consumo y costos por km (si se cargan los km)

## Estructura

```
public/index.html   La calculadora (HTML, CSS y JS en un solo archivo)
server.js           Servidor estático mínimo en Node, sin dependencias
package.json        Script de inicio para Railway
```

## Correr en local

```bash
npm start
# abrir http://localhost:3000
```

También se puede abrir `public/index.html` directamente en el navegador.

## Desplegar en Railway

1. En Railway: **New Project → Deploy from GitHub repo** y elegir este repositorio.
2. Railway detecta Node y ejecuta `npm start`. No hace falta configurar variables.
3. En **Settings → Networking → Generate Domain** para obtener la dirección pública.

Cada `git push` a la rama principal vuelve a desplegar la app automáticamente.
