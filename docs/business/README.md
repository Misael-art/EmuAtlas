# Produto e modelo de negócio

EmuAtlas organiza o trabalho que transforma uma biblioteca do cliente em um ambiente compreensível, portátil e mantível. O valor cobrado é organização, automação, adaptação de experiência, instalação e suporte, com evidências do que funciona em cada máquina.

## Filosofia

1. Uma única origem para cada conteúdo. Aliases relativos dão compatibilidade a consumidores; cópias desnecessárias não são estratégia de integração.
2. A estrutura nasce de funções: jogos, mídias, BIOS, saves, metadados, configuração, ferramentas e evidências possuem lugares definidos.
3. Nenhuma inferência vira verdade silenciosamente. Nome parecido, download completo, runtime instalado e jogo testado são evidências diferentes.
4. Automatizar o que pode ser repetido com segurança. Mudanças em conteúdo precisam plano e reversão. Instalação de estrutura vazia pode ser repetida sem destruir personalizações.
5. A experiência inclui entrar, navegar, jogar, sair e recuperar o desktop. A aceitação termina depois do retorno dos controles, áudio e tela.
6. O cliente mantém o controle da biblioteca. Dados ficam locais; não há upload ou telemetria no bootstrap.
7. Aparência segue a proposta de cada tema. Bibliotecas de arte são classificadas e registradas; autoria, licença e proveniência acompanham os assets.

## Oferta inicial proposta

| Oferta | Entrega | Critério de aceite |
| --- | --- | --- |
| Implantação piloto | Diagnóstico, estrutura limpa, dependências selecionadas e integração de biblioteca fornecida pelo cliente | Relatório de instalação e plataformas contratadas verificadas |
| Curadoria e organização | Inventário, identificação, nomes, variantes e mídia por planos reversíveis | Manifestos e ausência de perda de conteúdo |
| Experiência de frontend | Adaptação de tema, navegação, controle e resoluções contratadas | Jornada física completa aprovada na máquina alvo |
| Manutenção recorrente | Atualizações revisadas, backup e regressão do escopo contratado | Versões registradas, rollback e relatório de alterações |

Não há preço inventado nem assinatura ativa. Precificar depois de medir horas de implantação, custo de suporte por máquina, armazenamento de releases, aquisição de artes/licenças e manutenção de adaptadores. Evitar prometer cobertura irrestrita de plataformas num preço único. Um inventário grande não é uma matriz de compatibilidade homologada.

## Fluxo operacional

Diagnóstico sem alteração → escopo e orçamento por host/plataforma → instalação reproduzível → importação autorizada de dados → organização com plano → testes físicos → entrega e orientação → manutenção contratada.

Papéis: responsável pelo produto define escopo e suporte; engenharia mantém instaladores/rollback; curadoria confirma identidades e mídias; design valida navegação/visual/som; operador registra aceite; atendimento acompanha incidentes. Uma pessoa pode acumular papéis, mas os registros distinguem decisão, implementação e aprovação.

## Limites da versão inicial

O pacote contém ferramentas e templates, além do instalador de 59 componentes do host. Não contém a biblioteca pessoal nem reconstrói automaticamente todos os temas/BIOS/perfis particulares. Runtimes adicionais reutilizam `emutool`/runtime_install por receitas pinadas, com rastreio de dependências. A obtenção automática de mídia exige uma origem licenciada/configurada por cliente. O binário RetroFE possui receita de compilação; temas comerciais continuam dependendo de direitos e insumos aprovados. Switch 2 permanece experimental e fora da oferta homologada.

Documentos técnicos: [arquitetura](architecture.md), [operações](operations.md), [distribuição](distribution-policy.md), [qualidade](quality-and-roadmap.md), [release](releasing.md).
