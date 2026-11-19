# Politique de confidentialité — Jarvis

*Dernière mise à jour : 19 novembre 2026*

Jarvis est un assistant personnel installé sur l'ordinateur de son propriétaire. Il n'est utilisé que par cette personne.

## Données utilisées
- **Google Agenda** : Jarvis lit les rendez-vous pour les rappeler et, sur demande et après confirmation, en ajoute, en déplace ou en supprime.
- **Courriels, rappels, listes et mémoire** : ils servent uniquement à répondre aux demandes du propriétaire.

## Où vont les données
- Tout est traité et stocké **sur l'ordinateur du propriétaire**, par exemple dans `~/.openjarvis/`. Les clés d'accès sont protégées pour que lui seul puisse les lire.
- Aucune donnée n'est vendue, partagée ni envoyée à un serveur appartenant à l'auteur de Jarvis.
- Seuls ces services tiers reçoivent des données, uniquement pour faire leur travail :
  - Google, pour l'agenda et Gmail ;
  - Telegram, pour les messages ;
  - Microsoft Edge TTS, qui reçoit le texte des réponses pour fabriquer la voix ;
  - Open-Meteo, Radio-Canada, OpenStreetMap et DuckDuckGo, pour la météo, les nouvelles, les trajets et les recherches.

## Révoquer l'accès
L'accès à Google peut être retiré à tout moment sur https://myaccount.google.com/permissions. Il suffit aussi de supprimer `~/.openjarvis/google_token.json`.

## Contact
Par l'intermédiaire du dépôt GitHub de ce projet.
