v0.0.7 — Extração de entidades para o Agente 2 (V7)
Data: não registrada Acurácia medida: não medida nesta rodada de validação. Estado: protótipo de linha de comando — última versão antes da API (v0.1.0).
Propósito
Preparar a saída do Agente 1 para alimentar o Agente 2 (priorização jurídico-fiscal), que até aqui não existia como consumidor formal.
Componentes principais
A lógica de triagem (OCR, tipo de processo, citação, penhora, extinção, parcelamento) é idêntica à v0.0.6.
Adiciona extract_entidades_agente2(): extrai numero_processo, cpf_cnpj, nome_executado, nome_exequente, tipo_tributo, exercicio, numero_cda, data_inscricao, valor_original, valor_atualizado e vara — cada campo por regex com múltiplos padrões de fallback, retornando None quando não encontra (nunca inventa valor).
Planilha Excel ganha as colunas A2_*, com CPF/CNPJ e número de CDA forçados a formato texto (evita que o Excel corrompa esses números).
Decisão arquitetônica chave
Separar claramente extração (Agente 1) de decisão de priorização (Agente 2 — ainda não implementado nesta versão), preparando o contrato de dados entre os dois sem ainda ter o consumidor pronto. Esta separação intencional é a mesma citada nas notas da v0.1.0 do repositório ("decisão arquitetônica chave: a separação entre extração e decisão permite auditar cada componente independentemente").
