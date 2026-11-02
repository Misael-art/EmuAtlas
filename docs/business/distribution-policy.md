# Política de distribuição e proveniência

Esta prévia é privada. Não atribui uma licença proprietária ou aberta a todo o código legado: a autoria/origem e as permissões comerciais ainda precisam ser consolidadas por componente antes de entregar um produto licenciado.

| Material | Tratamento inicial |
| --- | --- |
| Novo código de distribuição e documentação | Desenvolvimento privado; modelo de licença comercial pendente da titularidade e decisão do responsável |
| Ferramentas existentes integradas | Revisão de origem/autoria pendente; disponibilidade interna não é autorização de redistribuição comercial |
| Emuladores/frontends terceiros | Receitas e aquisição na origem; payloads fora do pacote |
| Temas, fontes, logos, sons e arte | Nenhuma exportação automática; exigir licença e atribuições por componente |
| ROMs, BIOS, firmware, chaves e saves | Fora do repositório e do download de setup |
| Conteúdo e histórico do cliente | Local e fora do pacote |

O negócio vende trabalho, organização e suporte conforme o escopo contratado. Não presumir direito de redistribuir um executável, tema ou logo porque ele é gratuito, foi encontrado na internet ou está no disco do desenvolvedor. Aquisição direta e redistribuição são operações distintas.

Um registro aprovado precisa conter: componente, titular/origem, URL de licença/termos, versão, hash, permissão de aquisição, permissão de redistribuição quando aplicável, avisos/atribuição e eventual obrigação de fornecer fontes. A lista inicial em [component-review.json](../../tools/distribution/config/component-review.json) não substitui essas evidências nem uma licença final.

Não embutir dados pessoais em templates, exemplos ou logs de CI. A release usa allowlist, verifica ausência de payloads/symlinks/caminhos pessoais e publica inventário por SHA-256. O verificador não certifica sozinho ausência universal de segredos: revisar o diff e a seleção antes de publicar. Nenhum scanner de arquivos pode conceder direitos autorais.

O repositório deve começar privado, sem forks públicos, sem credenciais e sem expor builds da biblioteca operacional. Uma mudança para público ou uma primeira venda com redistribuição requer encerrar os itens de titularidade, licenças e aceitação que estão na matriz de prontidão.
